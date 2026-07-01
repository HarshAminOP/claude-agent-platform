"""CAP — Claude Agent Platform CLI.

Entry point: cap
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(stderr=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cap_home() -> Path:
    return Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))


def _data_dir() -> Path:
    return _cap_home() / "data"


def _resolve_workspace(workspace: str) -> str:
    return os.path.abspath(os.path.expanduser(workspace))


def _db_info(path: Path) -> dict:
    """Return size, table_count, last_modified for a database path."""
    info: dict = {
        "exists": path.exists(),
        "size_bytes": 0,
        "size_human": "—",
        "table_count": 0,
        "last_modified": "—",
    }
    if not path.exists():
        return info

    stat = path.stat()
    info["size_bytes"] = stat.st_size
    info["last_modified"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Human-readable size
    size = stat.st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            info["size_human"] = f"{size:.1f} {unit}"
            break
        size /= 1024
    else:
        info["size_human"] = f"{size:.1f} TB"

    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA busy_timeout=2000")
        rows = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        info["table_count"] = rows[0] if rows else 0
        conn.close()
    except Exception:
        pass

    return info


def _load_claude_json() -> dict:
    """Return parsed ~/.claude.json (or empty dict on failure)."""
    path = Path.home() / ".claude.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _status_color(status: str) -> str:
    mapping = {
        "running": "green",
        "registered": "cyan",
        "healthy": "green",
        "stopped": "red",
        "dead": "red",
        "completed": "blue",
        "failed": "red",
        "killed": "yellow",
        "pending": "dim",
        "not_started": "dim",
    }
    color = mapping.get(status.lower(), "white")
    return f"[{color}]{status}[/{color}]"


# ── Root group ─────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(version="2.0.0")
def cli():
    """CAP — Claude Agent Platform"""
    pass


# Register lifecycle commands
from cap.cli.lifecycle import init, uninstall, backup, restore
cli.add_command(init)
cli.add_command(uninstall)
cli.add_command(backup)
cli.add_command(restore)

# Register eval commands
from cap.eval.cli import eval_group
cli.add_command(eval_group)

# Register orchestration commands
from cap.cli.commands import health, dlq, resume, orchestrator_status, doctor
cli.add_command(health)
cli.add_command(dlq)
cli.add_command(resume)
cli.add_command(orchestrator_status, name="orch-status")
cli.add_command(doctor)

# Register witness commands
from cap.cli.witness import witness
cli.add_command(witness)


# ── cap config ─────────────────────────────────────────────────────────────────

@cli.group()
def config():
    """View and modify CAP configuration."""
    pass


@config.command("show")
def config_show():
    """Display current CAP configuration."""
    config_path = Path.home() / ".claude-platform" / "harness-config.json"
    if not config_path.exists():
        console.print("[yellow]No configuration found. Run `cap init` first.[/yellow]")
        return

    data = json.loads(config_path.read_text())

    table = Table(title="CAP Configuration", box=box.ROUNDED, show_edge=True)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")

    def _flatten(d, prefix=""):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _flatten(v, key)
            else:
                table.add_row(key, str(v))

    _flatten(data)
    console.print(table)
    console.print(f"\n[dim]Config file: {config_path}[/dim]")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a configuration value (dot-notation: aws.profile, budget.daily_limit_usd)."""
    config_path = Path.home() / ".claude-platform" / "harness-config.json"
    if not config_path.exists():
        console.print("[yellow]No configuration found. Run `cap init` first.[/yellow]")
        raise SystemExit(1)

    data = json.loads(config_path.read_text())

    parts = key.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]

    final_key = parts[-1]
    old_value = target.get(final_key)
    if isinstance(old_value, float):
        target[final_key] = float(value)
    elif isinstance(old_value, int):
        target[final_key] = int(value)
    elif isinstance(old_value, bool):
        target[final_key] = value.lower() in ("true", "1", "yes")
    else:
        target[final_key] = value

    config_path.write_text(json.dumps(data, indent=2) + "\n")
    console.print(f"[green]✓[/green] Set {key} = {target[final_key]}")


# ── cap status ─────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Platform health overview."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_platform_db, init_knowledge_db, init_sessions_db, init_fleet_db

    config = load_config()
    data_dir = config.data_dir

    from cap import __version__ as cap_version
    console.print(Panel(
        f"[bold cyan]CAP — Claude Agent Platform[/bold cyan]  [dim]v{cap_version}[/dim]\n"
        f"[dim]Home:[/dim] {config.home}\n"
        f"[dim]Data:[/dim] {data_dir}",
        title="Platform",
        box=box.ROUNDED,
    ))

    # ── Databases ──────────────────────────────────────────────────────────────
    db_table = Table(title="Databases", box=box.SIMPLE_HEAD, show_edge=False)
    db_table.add_column("Database", style="bold")
    db_table.add_column("Size", justify="right")
    db_table.add_column("Tables", justify="right")
    db_table.add_column("Last Modified")
    db_table.add_column("Status")

    db_specs = [
        ("platform.db", "workflow engine + budget"),
        ("knowledge.db", "knowledge base"),
        ("sessions.db", "session memory"),
        ("fleet.db", "MCP fleet"),
    ]

    for db_name, _ in db_specs:
        info = _db_info(data_dir / db_name)
        db_status = "[green]ok[/green]" if info["exists"] else "[red]missing[/red]"
        db_table.add_row(
            db_name,
            info["size_human"],
            str(info["table_count"]) if info["exists"] else "—",
            info["last_modified"],
            db_status,
        )

    console.print(db_table)

    # ── MCP Servers ────────────────────────────────────────────────────────────
    claude_data = _load_claude_json()
    mcp_servers = claude_data.get("mcpServers", {})

    # Also check fleet.db for running status
    fleet_pids: dict[str, int | None] = {}
    fleet_statuses: dict[str, str] = {}
    fleet_path = data_dir / "fleet.db"
    if fleet_path.exists():
        try:
            conn = sqlite3.connect(str(fleet_path))
            conn.execute("PRAGMA busy_timeout=2000")
            rows = conn.execute("SELECT name, pid, status FROM fleet_servers").fetchall()
            for row in rows:
                fleet_pids[row[0]] = row[1]
                fleet_statuses[row[0]] = row[2]
            conn.close()
        except Exception:
            pass

    if mcp_servers:
        mcp_table = Table(title="MCP Servers (from ~/.claude.json)", box=box.SIMPLE_HEAD, show_edge=False)
        mcp_table.add_column("Name", style="bold")
        mcp_table.add_column("Command")
        mcp_table.add_column("Fleet Status")

        for name, cfg in mcp_servers.items():
            command = cfg.get("command", "")
            args_list = cfg.get("args", [])
            cmd_display = f"{command} {' '.join(str(a) for a in args_list[:2])}" if args_list else command
            cmd_display = cmd_display[:60]

            if name in fleet_statuses:
                fleet_st = fleet_statuses[name]
                pid = fleet_pids.get(name)
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                    fleet_cell = _status_color("running" if alive else "stopped")
                else:
                    fleet_cell = _status_color(fleet_st)
            else:
                fleet_cell = "[dim]registered (not fleet-managed)[/dim]"

            mcp_table.add_row(name, cmd_display, fleet_cell)

        console.print(mcp_table)
    else:
        console.print("[dim]No MCP servers configured in ~/.claude.json[/dim]\n")

    # ── Knowledge ──────────────────────────────────────────────────────────────
    knowledge_path = data_dir / "knowledge.db"
    if knowledge_path.exists():
        try:
            conn = sqlite3.connect(str(knowledge_path))
            conn.execute("PRAGMA busy_timeout=2000")
            total_k = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
            embedded_k = conn.execute(
                "SELECT COUNT(*) FROM knowledge_entries WHERE embedding_status = 'embedded'"
            ).fetchone()[0]
            bk_count = conn.execute("SELECT COUNT(*) FROM business_knowledge").fetchone()[0]
            conn.close()
            coverage = round(embedded_k / max(total_k, 1) * 100, 1)
            console.print(
                f"[bold]Knowledge[/bold]  {total_k} entries  "
                f"embedding coverage [cyan]{coverage}%[/cyan]  "
                f"business knowledge [cyan]{bk_count}[/cyan] entries"
            )
        except Exception as exc:
            console.print(f"[yellow]Knowledge DB: could not query ({exc})[/yellow]")
    else:
        console.print("[dim]Knowledge DB: not initialized[/dim]")

    # ── Sessions ───────────────────────────────────────────────────────────────
    sessions_path = data_dir / "sessions.db"
    if sessions_path.exists():
        try:
            conn = sqlite3.connect(str(sessions_path))
            conn.execute("PRAGMA busy_timeout=2000")
            active_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
            ).fetchone()[0]
            total_learnings = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
            conn.close()
            console.print(
                f"[bold]Sessions[/bold]   {active_sessions} active  "
                f"[cyan]{total_learnings}[/cyan] total learnings"
            )
        except Exception as exc:
            console.print(f"[yellow]Sessions DB: could not query ({exc})[/yellow]")
    else:
        console.print("[dim]Sessions DB: not initialized[/dim]")

    # ── Budget ─────────────────────────────────────────────────────────────────
    platform_path = data_dir / "platform.db"
    if platform_path.exists():
        try:
            conn = sqlite3.connect(str(platform_path))
            conn.execute("PRAGMA busy_timeout=2000")
            now = datetime.now(timezone.utc)
            period = now.strftime("%Y-%m")
            row = conn.execute(
                "SELECT SUM(total_cost_usd) FROM budget_ledger WHERE period = ?",
                (period,)
            ).fetchone()
            monthly_spend = row[0] or 0.0
            cap = config.budget.monthly_cap_usd
            pct = round(monthly_spend / max(cap, 0.01) * 100, 1)
            color = "green" if pct < 60 else ("yellow" if pct < config.budget.warning_threshold * 100 else "red")
            console.print(
                f"[bold]Budget[/bold]     ${monthly_spend:.2f} / ${cap:.2f} "
                f"([{color}]{pct}%[/{color}]) this month"
            )
            conn.close()
        except Exception as exc:
            console.print(f"[yellow]Budget: could not query ({exc})[/yellow]")
    else:
        console.print("[dim]Budget: platform DB not initialized[/dim]")

    # ── Trust Levels ──────────────────────────────────────────────────────────
    cap_db_path = Path(os.path.expanduser("~/.cap/cap.db"))
    if cap_db_path.exists():
        try:
            conn = sqlite3.connect(str(cap_db_path))
            conn.execute("PRAGMA busy_timeout=2000")
            trust_rows = conn.execute(
                "SELECT agent_type, action_type, trust_score, success_count, failure_count "
                "FROM trust_levels ORDER BY trust_score DESC LIMIT 10"
            ).fetchall()
            conn.close()
            if trust_rows:
                trust_table = Table(title="Trust Levels", box=box.SIMPLE_HEAD, show_edge=False)
                trust_table.add_column("Agent", style="bold")
                trust_table.add_column("Action")
                trust_table.add_column("Trust", justify="right")
                trust_table.add_column("Success", justify="right")
                trust_table.add_column("Failure", justify="right")
                for row in trust_rows:
                    score = row[2] if isinstance(row, (tuple, list)) else row["trust_score"]
                    t_color = "green" if score >= 0.7 else ("yellow" if score >= 0.4 else "red")
                    trust_table.add_row(
                        row[0], row[1],
                        f"[{t_color}]{score:.2f}[/{t_color}]",
                        str(row[3]), str(row[4]),
                    )
                console.print(trust_table)
        except Exception:
            pass

    # ── Mode ──────────────────────────────────────────────────────────────────
    try:
        from cap.db import get_db as _get_cap_db, migrate as _cap_migrate
        cap_conn = _get_cap_db(str(cap_db_path))
        _cap_migrate(cap_conn)
        from cap.cost.tracker import CostTracker
        tracker = CostTracker(cap_conn)
        budget_info = tracker.budget_check()
        mode = budget_info["mode"]
        mode_color = {"online": "green", "degraded": "yellow", "offline": "red"}.get(mode, "white")
        console.print(f"\n[bold]Mode:[/bold] [{mode_color}]{mode}[/{mode_color}]")
        cap_conn.close()
    except Exception:
        pass


# ── cap doctor ─────────────────────────────────────────────────────────────────

@cli.command("db-doctor")
@click.option("--fix", is_flag=True, help="Attempt fixes (dry-run without --yes)")
@click.option("--yes", is_flag=True, help="Actually apply fixes")
@click.option("--db", "db_filter", type=str, default=None, help="Check specific database only")
def db_doctor(fix: bool, yes: bool, db_filter: str | None):
    """Diagnose and repair database integrity issues."""
    from cap.lib.config import load_config
    from cap.lib.db_maintenance import DBMaintenance

    config = load_config()
    data_dir = config.data_dir

    apply = fix and yes
    dry_run = fix and not yes

    if dry_run:
        console.print("[yellow]Dry-run mode: showing what WOULD be fixed. Pass --yes to apply.[/yellow]\n")

    all_dbs = ["platform.db", "knowledge.db", "sessions.db", "fleet.db"]
    target_dbs = [db_filter] if db_filter else all_dbs

    maintenance = DBMaintenance(data_dir)

    issues_table = Table(title="Doctor Report", box=box.SIMPLE_HEAD, show_edge=False)
    issues_table.add_column("Database", style="bold")
    issues_table.add_column("Check")
    issues_table.add_column("Result")
    issues_table.add_column("Action")

    any_issues = False

    for db_name in target_dbs:
        db_path = data_dir / db_name
        result = maintenance.doctor(db_path, fix=apply)

        if not result["issues"]:
            issues_table.add_row(db_name, "all checks", "[green]ok[/green]", "—")
        else:
            any_issues = True
            for issue in result["issues"]:
                issues_table.add_row(db_name, "issue", f"[red]{issue}[/red]", "")

            if apply and result.get("actions_taken"):
                for action in result["actions_taken"]:
                    issues_table.add_row("", "fixed", f"[green]{action}[/green]", "")
            elif dry_run and result.get("would_do"):
                for would in result["would_do"]:
                    issues_table.add_row("", "would fix", f"[yellow]{would}[/yellow]", "")

    console.print(issues_table)

    if not any_issues:
        console.print("\n[green]All databases healthy.[/green]")
    elif not fix:
        console.print("\n[dim]Run with --fix to see proposed fixes, or --fix --yes to apply.[/dim]")
    elif dry_run:
        console.print("\n[dim]Re-run with --fix --yes to apply the above fixes.[/dim]")
    else:
        console.print("\n[green]Fixes applied.[/green]")


# ── cap knowledge ──────────────────────────────────────────────────────────────

@cli.group()
def knowledge():
    """Manage knowledge base."""
    pass


@knowledge.command("search")
@click.argument("query")
@click.option("--workspace", "-w", default=".", help="Workspace path")
@click.option(
    "--strategy",
    type=click.Choice(["hybrid", "keyword", "semantic", "graph"]),
    default="hybrid",
    show_default=True,
)
@click.option("--top-k", "-k", "top_k", type=int, default=10, show_default=True)
def knowledge_search(query: str, workspace: str, strategy: str, top_k: int):
    """Search the knowledge base."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.retrieval import hybrid_search

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    with console.status(f"[cyan]Searching ({strategy})…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)
            results = hybrid_search(
                conn=db,
                vectors_table=None,
                query=query,
                query_vector=None,
                workspace=workspace,
                strategy=strategy,
                top_k=top_k,
            )
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            raise SystemExit(1)

    if not results:
        console.print("[dim]No results found.[/dim]")
        return

    table = Table(
        title=f"Results for '{query}'  ({len(results)} of {top_k} max)",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Type", width=12)
    table.add_column("Title")
    table.add_column("Preview", max_width=60)

    for i, r in enumerate(results, 1):
        table.add_row(
            str(i),
            f"{r.score:.3f}",
            r.content_type or "—",
            r.title or "—",
            (r.content_preview or "")[:80],
        )

    console.print(table)


@knowledge.command("add")
@click.option(
    "--category", "-c",
    required=True,
    type=click.Choice(["team", "ownership", "convention", "deadline", "glossary", "incident"]),
    help="Knowledge category",
)
@click.option("--key", "-k", "key", required=True, help="Unique key")
@click.option("--value", "-v", "value", required=True, help="Content")
@click.option("--workspace", "-w", default=".", help="Workspace path")
def knowledge_add(category: str, key: str, value: str, workspace: str):
    """Add a business knowledge entry."""
    import uuid as _uuid
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.security import sanitize_content

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Writing…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)
            safe_value = sanitize_content(value)
            bk_id = str(_uuid.uuid4())
            db.execute(
                """INSERT INTO business_knowledge (id, workspace, category, key, value, source)
                   VALUES (?, ?, ?, ?, ?, 'cli')
                   ON CONFLICT(workspace, category, key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = datetime('now')""",
                (bk_id, workspace, category, key, safe_value),
            )
            db.commit()
        except Exception as exc:
            console.print(f"[red]Failed to add knowledge: {exc}[/red]")
            raise SystemExit(1)

    console.print(f"[green]Recorded[/green]  [{category}] {key}")


@knowledge.command("status")
@click.option("--workspace", "-w", default=None, help="Filter to workspace")
def knowledge_status(workspace: str | None):
    """Show knowledge base statistics."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db

    config = load_config()
    data_dir = config.data_dir

    resolved = _resolve_workspace(workspace) if workspace else None

    with console.status("[cyan]Querying…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)

            where = "WHERE workspace = ?" if resolved else ""
            params: tuple = (resolved,) if resolved else ()
            and_or = "AND" if resolved else "WHERE"

            total = db.execute(f"SELECT COUNT(*) FROM knowledge_entries {where}", params).fetchone()[0]
            embedded = db.execute(
                f"SELECT COUNT(*) FROM knowledge_entries {where} {and_or} embedding_status = 'embedded'",
                params,
            ).fetchone()[0]
            pending_q = db.execute(
                "SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'"
            ).fetchone()[0]
            failed_q = db.execute(
                "SELECT COUNT(*) FROM embedding_queue WHERE status = 'failed'"
            ).fetchone()[0]
            graph_nodes = db.execute(
                f"SELECT COUNT(*) FROM knowledge_graph_nodes {where}", params
            ).fetchone()[0]
            graph_edges = db.execute(
                f"SELECT COUNT(*) FROM knowledge_graph_edges {where}", params
            ).fetchone()[0]
            bk_count = db.execute(
                f"SELECT COUNT(*) FROM business_knowledge {where}", params
            ).fetchone()[0]

            by_type = db.execute(
                f"SELECT content_type, COUNT(*) FROM knowledge_entries {where} GROUP BY content_type",
                params,
            ).fetchall()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    coverage = round(embedded / max(total, 1) * 100, 1)

    console.print(Panel(
        f"Total entries:   [cyan]{total}[/cyan]\n"
        f"Embedded:        [cyan]{embedded}[/cyan]  ({coverage}% coverage)\n"
        f"Queue pending:   [cyan]{pending_q}[/cyan]\n"
        f"Queue failed:    [{'red' if failed_q else 'dim'}]{failed_q}[/{'red' if failed_q else 'dim'}]\n"
        f"Graph nodes:     [cyan]{graph_nodes}[/cyan]\n"
        f"Graph edges:     [cyan]{graph_edges}[/cyan]\n"
        f"Business KV:     [cyan]{bk_count}[/cyan]",
        title=f"Knowledge Status{' — ' + workspace if workspace else ''}",
        box=box.ROUNDED,
    ))

    if by_type:
        t = Table(title="By Content Type", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Type", style="bold")
        t.add_column("Count", justify="right")
        for row in sorted(by_type, key=lambda r: r[1], reverse=True):
            t.add_row(row[0], str(row[1]))
        console.print(t)


# ── cap session ────────────────────────────────────────────────────────────────

@cli.group()
def session():
    """Session memory management."""
    pass


@session.command("list")
@click.option("--workspace", "-w", default=None, help="Filter to workspace")
@click.option("--limit", "-n", type=int, default=20, show_default=True)
def session_list(workspace: str | None, limit: int):
    """List past sessions."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_sessions_db

    config = load_config()
    data_dir = config.data_dir
    resolved = _resolve_workspace(workspace) if workspace else None

    with console.status("[cyan]Loading…[/cyan]"):
        try:
            db = init_sessions_db(data_dir)
            if resolved:
                rows = db.execute(
                    "SELECT id, workspace, started_at, ended_at, status, summary "
                    "FROM sessions WHERE workspace = ? ORDER BY started_at DESC LIMIT ?",
                    (resolved, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, workspace, started_at, ended_at, status, summary "
                    "FROM sessions ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not rows:
        console.print("[dim]No sessions found.[/dim]")
        return

    t = Table(title=f"Sessions (last {limit})", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("ID", style="dim", width=12)
    t.add_column("Workspace", max_width=30)
    t.add_column("Started")
    t.add_column("Ended")
    t.add_column("Status")
    t.add_column("Summary", max_width=40)

    for row in rows:
        sid, ws, started, ended, st, summary = row
        t.add_row(
            sid[:12],
            (ws or "—")[-30:],
            (started or "—")[:16],
            (ended or "—")[:16],
            _status_color(st or "unknown"),
            (summary or "")[:40],
        )

    console.print(t)


@session.command("recall")
@click.argument("query")
@click.option("--workspace", "-w", default=".", help="Workspace path")
def session_recall(query: str, workspace: str):
    """Search session memory for past decisions and learnings."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_sessions_db

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Searching session memory…[/cyan]"):
        try:
            db = init_sessions_db(data_dir)

            # FTS5 decisions search
            try:
                decisions = db.execute(
                    "SELECT d.id, d.domain, d.decision, d.rationale, d.created_at "
                    "FROM decisions d "
                    "JOIN decisions_fts df ON d.rowid = df.rowid "
                    "WHERE decisions_fts MATCH ? AND d.workspace = ? "
                    "ORDER BY rank LIMIT 10",
                    (query, workspace),
                ).fetchall()
            except Exception:
                decisions = db.execute(
                    "SELECT id, domain, decision, rationale, created_at "
                    "FROM decisions WHERE workspace = ? AND decision LIKE ? "
                    "ORDER BY created_at DESC LIMIT 10",
                    (workspace, f"%{query}%"),
                ).fetchall()

            learnings = db.execute(
                "SELECT category, key, value, confidence "
                "FROM learnings "
                "WHERE (workspace = ? OR workspace IS NULL) AND (key LIKE ? OR value LIKE ?) "
                "ORDER BY confidence DESC LIMIT 10",
                (workspace, f"%{query}%", f"%{query}%"),
            ).fetchall()

            corrections = db.execute(
                "SELECT what_was_wrong, what_is_correct, category "
                "FROM corrections "
                "WHERE (workspace = ? OR workspace IS NULL) AND (what_was_wrong LIKE ? OR what_is_correct LIKE ?) "
                "ORDER BY created_at DESC LIMIT 5",
                (workspace, f"%{query}%", f"%{query}%"),
            ).fetchall()
        except Exception as exc:
            console.print(f"[red]Recall failed: {exc}[/red]")
            raise SystemExit(1)

    if decisions:
        t = Table(title="Decisions", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Domain", style="bold", width=14)
        t.add_column("Decision", max_width=55)
        t.add_column("Rationale", max_width=35)
        t.add_column("Date", width=10)
        for row in decisions:
            _, domain, decision, rationale, created = row
            t.add_row(domain or "—", decision, rationale or "—", (created or "")[:10])
        console.print(t)

    if learnings:
        t = Table(title="Learnings", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Category", style="bold", width=14)
        t.add_column("Key", width=20)
        t.add_column("Value", max_width=60)
        t.add_column("Conf.", justify="right", width=6)
        for row in learnings:
            category, key, value, confidence = row
            t.add_row(category, key, value, f"{confidence:.2f}")
        console.print(t)

    if corrections:
        t = Table(title="Corrections", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Wrong", max_width=45)
        t.add_column("Correct", max_width=45)
        t.add_column("Category", width=12)
        for row in corrections:
            wrong, correct, category = row
            t.add_row(wrong, correct, category or "—")
        console.print(t)

    if not decisions and not learnings and not corrections:
        console.print("[dim]No matching session memory found.[/dim]")


@session.command("learnings")
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option("--workspace", "-w", default=".", help="Workspace path")
def session_learnings(category: str | None, workspace: str):
    """List recorded learnings."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_sessions_db

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Loading learnings…[/cyan]"):
        try:
            db = init_sessions_db(data_dir)
            if category:
                rows = db.execute(
                    "SELECT category, key, value, confidence, times_applied, last_applied_at "
                    "FROM learnings "
                    "WHERE (workspace = ? OR workspace IS NULL) AND category = ? "
                    "ORDER BY confidence DESC, last_applied_at DESC",
                    (workspace, category),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT category, key, value, confidence, times_applied, last_applied_at "
                    "FROM learnings "
                    "WHERE (workspace = ? OR workspace IS NULL) "
                    "ORDER BY confidence DESC, last_applied_at DESC",
                    (workspace,),
                ).fetchall()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not rows:
        console.print("[dim]No learnings found.[/dim]")
        return

    title = f"Learnings{' [' + category + ']' if category else ''} — {len(rows)} total"
    t = Table(title=title, box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Category", style="bold", width=14)
    t.add_column("Key", width=22)
    t.add_column("Value", max_width=60)
    t.add_column("Conf.", justify="right", width=6)
    t.add_column("Applied", justify="right", width=7)

    for row in rows:
        cat, key, value, confidence, times_applied, _ = row
        t.add_row(cat, key, value, f"{confidence:.2f}", str(times_applied or 0))

    console.print(t)


# ── cap fleet ──────────────────────────────────────────────────────────────────

@cli.group()
def fleet():
    """MCP server fleet management."""
    pass


@fleet.command("status")
@click.option("--name", "-n", default=None, help="Specific server name")
def fleet_status(name: str | None):
    """Show fleet server status."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_fleet_db

    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Loading fleet status…[/cyan]"):
        try:
            db = init_fleet_db(data_dir)
            if name:
                rows = db.execute(
                    "SELECT name, command, status, pid, last_health_check, restart_count, max_restarts "
                    "FROM fleet_servers WHERE name = ?",
                    (name,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT name, command, status, pid, last_health_check, restart_count, max_restarts "
                    "FROM fleet_servers ORDER BY name",
                ).fetchall()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not rows:
        console.print("[dim]No fleet servers registered.[/dim]")
        return

    t = Table(title="Fleet Status", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Name", style="bold")
    t.add_column("Status")
    t.add_column("PID", justify="right")
    t.add_column("Alive")
    t.add_column("Restarts", justify="right")
    t.add_column("Last Check")
    t.add_column("Command", max_width=40)

    for row in rows:
        srv_name, command, srv_status, pid, last_check, restart_count, max_restarts = row
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
        alive_cell = "[green]yes[/green]" if alive else ("[dim]—[/dim]" if not pid else "[red]no[/red]")
        restarts_color = "red" if restart_count and restart_count >= max_restarts else "default"
        t.add_row(
            srv_name,
            _status_color(srv_status or "unknown"),
            str(pid) if pid else "—",
            alive_cell,
            f"[{restarts_color}]{restart_count or 0}/{max_restarts or 5}[/{restarts_color}]",
            (last_check or "never")[:16],
            (command or "")[:40],
        )

    console.print(t)


@fleet.command("discover")
@click.option("--workspace", "-w", default=".", help="Workspace path to scan")
def fleet_discover(workspace: str):
    """Discover MCP servers from workspace and global config."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_fleet_db

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Discovering servers…[/cyan]"):
        try:
            db = init_fleet_db(data_dir)
            discovered = []

            search_paths = [
                Path(workspace) / ".claude.json",
                Path.home() / ".claude.json",
            ]

            for path in search_paths:
                if path.exists():
                    try:
                        data = json.loads(path.read_text())
                        for srv_name, cfg in data.get("mcpServers", {}).items():
                            if srv_name.startswith("cap-"):
                                continue
                            existing = db.execute(
                                "SELECT name FROM fleet_servers WHERE name = ?", (srv_name,)
                            ).fetchone()
                            if not existing:
                                discovered.append({
                                    "name": srv_name,
                                    "command": cfg.get("command", ""),
                                    "args": cfg.get("args", []),
                                    "source": str(path),
                                })
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as exc:
            console.print(f"[red]Discovery failed: {exc}[/red]")
            raise SystemExit(1)

    if not discovered:
        console.print("[dim]No unmanaged servers found.[/dim]")
        return

    t = Table(title=f"Discovered {len(discovered)} unmanaged server(s)", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Name", style="bold")
    t.add_column("Command")
    t.add_column("Source")

    for s in discovered:
        args_preview = " ".join(str(a) for a in s["args"][:2])
        t.add_row(s["name"], f"{s['command']} {args_preview}".strip()[:50], s["source"])

    console.print(t)
    console.print("[dim]Use fleet_register (via MCP) to add servers to fleet management.[/dim]")


@fleet.command("health-check")
def fleet_health_check():
    """Run immediate health check on all registered fleet servers."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_fleet_db
    from datetime import datetime, timezone

    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Running health checks…[/cyan]"):
        try:
            db = init_fleet_db(data_dir)
            rows = db.execute(
                "SELECT name, pid, status FROM fleet_servers"
            ).fetchall()

            now = datetime.now(timezone.utc).isoformat()
            results = []

            for srv_name, pid, srv_status in rows:
                if srv_status == "registered" and not pid:
                    results.append({"name": srv_name, "healthy": None, "status": "not_started"})
                    continue

                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False

                health_status = "healthy" if alive else "dead"

                if not alive and srv_status == "running":
                    db.execute(
                        "UPDATE fleet_servers SET status = 'stopped' WHERE name = ?", (srv_name,)
                    )
                    db.execute(
                        "INSERT INTO fleet_events (server_name, event_type, message) VALUES (?, 'died', ?)",
                        (srv_name, f"health-check CLI: process {pid} not found"),
                    )

                db.execute(
                    "UPDATE fleet_servers SET last_health_check = ? WHERE name = ?", (now, srv_name)
                )
                results.append({"name": srv_name, "pid": pid, "status": health_status, "healthy": alive})

            db.commit()
        except Exception as exc:
            console.print(f"[red]Health check failed: {exc}[/red]")
            raise SystemExit(1)

    t = Table(title=f"Health Check — {now[:16]}", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Name", style="bold")
    t.add_column("PID", justify="right")
    t.add_column("Result")

    for r in results:
        if r["healthy"] is None:
            result_cell = "[dim]not started[/dim]"
        elif r["healthy"]:
            result_cell = "[green]healthy[/green]"
        else:
            result_cell = "[red]dead[/red]"
        t.add_row(r["name"], str(r.get("pid") or "—"), result_cell)

    console.print(t)


# ── cap workflow ───────────────────────────────────────────────────────────────

@cli.group()
def workflow():
    """Workflow management."""
    pass


@workflow.command("list")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["running", "completed", "failed", "killed", "all"]),
    default="all",
    show_default=True,
)
def workflow_list(status_filter: str):
    """List workflows."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_platform_db

    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Loading workflows…[/cyan]"):
        try:
            db = init_platform_db(data_dir)
            if status_filter == "all":
                rows = db.execute(
                    "SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, started_at, completed_at "
                    "FROM workflows ORDER BY started_at DESC LIMIT 50"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, name, status, budget_tokens, tokens_used, agents_spawned, started_at, completed_at "
                    "FROM workflows WHERE status = ? ORDER BY started_at DESC LIMIT 50",
                    (status_filter,),
                ).fetchall()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not rows:
        console.print("[dim]No workflows found.[/dim]")
        return

    t = Table(
        title=f"Workflows{' [' + status_filter + ']' if status_filter != 'all' else ''}",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )
    t.add_column("ID", style="dim", width=16)
    t.add_column("Name", max_width=30)
    t.add_column("Status")
    t.add_column("Budget %", justify="right")
    t.add_column("Agents", justify="right")
    t.add_column("Started")
    t.add_column("Completed")

    for row in rows:
        wf_id, wf_name, wf_status, budget, tokens_used, agents, started, completed = row
        pct = round(tokens_used / max(budget, 1) * 100, 1)
        pct_color = "green" if pct < 60 else ("yellow" if pct < 90 else "red")
        t.add_row(
            wf_id[:16],
            wf_name[:30],
            _status_color(wf_status or "unknown"),
            f"[{pct_color}]{pct}%[/{pct_color}]",
            str(agents or 0),
            (started or "—")[:16],
            (completed or "—")[:16],
        )

    console.print(t)


@workflow.command("status")
@click.argument("run_id")
def workflow_status(run_id: str):
    """Show detailed workflow status."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_platform_db

    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Loading…[/cyan]"):
        try:
            db = init_platform_db(data_dir)
            row = db.execute(
                "SELECT id, name, status, budget_tokens, max_agents, tokens_used, "
                "agents_spawned, killed, started_at, completed_at, error "
                "FROM workflows WHERE id = ? OR id LIKE ?",
                (run_id, f"{run_id}%"),
            ).fetchone()
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not row:
        console.print(f"[red]Workflow '{run_id}' not found.[/red]")
        raise SystemExit(1)

    wf_id, name, wf_status, budget, max_agents, tokens_used, agents_spawned, killed, started, completed, error = row
    pct = round(tokens_used / max(budget, 1) * 100, 1)

    console.print(Panel(
        f"[bold]Name:[/bold]     {name}\n"
        f"[bold]Status:[/bold]   {_status_color(wf_status or 'unknown')}\n"
        f"[bold]Budget:[/bold]   {tokens_used:,} / {budget:,} tokens ({pct}%)\n"
        f"[bold]Agents:[/bold]   {agents_spawned} / {max_agents} spawned\n"
        f"[bold]Started:[/bold]  {started or '—'}\n"
        f"[bold]Ended:[/bold]    {completed or '—'}\n"
        + (f"[bold]Error:[/bold]    [red]{error}[/red]\n" if error else ""),
        title=f"Workflow  [dim]{wf_id}[/dim]",
        box=box.ROUNDED,
    ))

    # Recent events
    try:
        events = db.execute(
            "SELECT event_type, phase, agent_id, message, tokens_delta, timestamp "
            "FROM workflow_events WHERE workflow_id = ? ORDER BY timestamp DESC LIMIT 15",
            (wf_id,),
        ).fetchall()
    except Exception:
        events = []

    if events:
        t = Table(title="Recent Events", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Time", width=16)
        t.add_column("Type", width=14)
        t.add_column("Phase")
        t.add_column("Agent", width=12)
        t.add_column("Message", max_width=50)
        t.add_column("Tokens", justify="right", width=8)

        for ev in events:
            ev_type, phase, agent_id, message, tokens_delta, timestamp = ev
            t.add_row(
                (timestamp or "")[:16],
                ev_type or "—",
                phase or "—",
                (agent_id or "—")[:12],
                message or "—",
                str(tokens_delta) if tokens_delta else "—",
            )
        console.print(t)


@workflow.command("watch")
@click.argument("workflow_id", required=False)
@click.option("--poll", "-p", type=float, default=2.0, show_default=True, help="Poll interval seconds")
def workflow_watch(workflow_id: str | None, poll: float):
    """Watch a workflow as team conversation (live)."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_platform_db
    from cap.lib.workflow_observer import WorkflowObserver

    config = load_config()
    data_dir = config.data_dir

    try:
        db = init_platform_db(data_dir)
        if not workflow_id:
            row = db.execute(
                "SELECT id, name FROM workflows WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT id, name FROM workflows ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            if not row:
                console.print("[dim]No workflows found.[/dim]")
                return
            workflow_id = row[0]
            console.print(f"[dim]Watching: {row[1]} ({workflow_id[:16]})[/dim]")
        db.close()
    except Exception as exc:
        console.print(f"[red]Failed: {exc}[/red]")
        raise SystemExit(1)

    observer = WorkflowObserver(data_dir / "platform.db", workflow_id)
    try:
        observer.watch(poll_interval=poll)
    except KeyboardInterrupt:
        observer.stop()
        console.print("\n[dim]Stopped.[/dim]")


@workflow.command("demo")
def workflow_demo():
    """Run demo team conversation rendering."""
    from cap.lib.team_renderer import demo_workflow
    demo_workflow()


@workflow.command("daemon")
@click.option("--poll", "-p", type=float, default=3.0, show_default=True, help="Poll interval seconds")
@click.option("--bg", is_flag=True, help="Run in background (daemonize)")
@click.pass_context
def workflow_daemon(ctx, poll: float, bg: bool):
    """Auto-watch new workflows as team conversations."""
    from cap.cli.daemon import daemon as _daemon_cmd
    ctx.invoke(_daemon_cmd, poll=poll, bg=bg)


@workflow.command("kill")
@click.argument("run_id")
@click.option("--reason", "-r", default="User requested kill", show_default=True)
def workflow_kill(run_id: str, reason: str):
    """Kill a running workflow."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_platform_db
    from datetime import datetime, timezone

    config = load_config()
    data_dir = config.data_dir

    with console.status("[cyan]Killing workflow…[/cyan]"):
        try:
            db = init_platform_db(data_dir)
            # Allow prefix match
            row = db.execute(
                "SELECT id, name, status FROM workflows WHERE id = ? OR id LIKE ?",
                (run_id, f"{run_id}%"),
            ).fetchone()

            if not row:
                console.print(f"[red]Workflow '{run_id}' not found.[/red]")
                raise SystemExit(1)

            wf_id, name, wf_status = row
            if wf_status in ("completed", "failed", "killed"):
                console.print(f"[yellow]Workflow '{wf_id}' is already {wf_status}.[/yellow]")
                return

            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE workflows SET killed = 1, status = 'killed', completed_at = ?, error = ? WHERE id = ?",
                (now, reason, wf_id),
            )
            db.execute(
                "INSERT INTO workflow_events (workflow_id, event_type, message, timestamp) VALUES (?, 'killed', ?, ?)",
                (wf_id, reason, now),
            )
            db.commit()
        except SystemExit:
            raise
        except Exception as exc:
            console.print(f"[red]Kill failed: {exc}[/red]")
            raise SystemExit(1)

    console.print(f"[red]Killed[/red]  {wf_id}  ({name})  — {reason}")


# ── cap budget ─────────────────────────────────────────────────────────────────

@cli.group()
def budget():
    """Budget and cost management."""
    pass


@budget.command("status")
@click.option("--workspace", "-w", default=None, help="Show per-project budget (if per_project enabled)")
def budget_status(workspace: str | None):
    """Show today's spend, remaining, top consumers, active/paused state."""
    from cap.lib.config import load_config
    from cap.lib.harness_config import load_harness_config
    from cap.lib.budget_manager import (
        init_budget_log_table, get_today_spend, get_top_consumers, is_budget_paused,
    )

    config = load_config()
    harness_cfg = load_harness_config()
    data_dir = config.data_dir
    budget_cfg = harness_cfg.get("budget", {})
    daily_limit = budget_cfg.get("daily_limit_usd", 5.0)
    agent_caps = budget_cfg.get("agent_caps", {})
    per_project = budget_cfg.get("per_project", False)

    resolved_ws = _resolve_workspace(workspace) if workspace else None

    try:
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        init_budget_log_table(db)

        ws_filter = resolved_ws if per_project else None
        spend_info = get_today_spend(db, workspace=ws_filter)
        top_consumers = get_top_consumers(db, n=5, workspace=ws_filter)
    except Exception as exc:
        console.print(f"[red]Query failed: {exc}[/red]")
        raise SystemExit(1)

    today_spend = spend_info["total_spend_usd"]
    remaining = max(daily_limit - today_spend, 0.0)
    pct = round(today_spend / max(daily_limit, 0.01) * 100, 1)
    color = "green" if pct < 60 else ("yellow" if pct < 80 else "red")
    paused = is_budget_paused()
    state = "[red]PAUSED[/red]" if paused else "[green]ACTIVE[/green]"

    console.print(Panel(
        f"Date:            [bold]{spend_info['date']}[/bold]\n"
        f"State:           {state}\n"
        f"Today's spend:   [bold][{color}]${today_spend:.4f}[/{color}][/bold] / ${daily_limit:.2f}  ({pct}%)\n"
        f"Remaining:       [cyan]${remaining:.4f}[/cyan]\n"
        f"Executions:      {spend_info['execution_count']}\n"
        f"Per-project:     {'enabled' if per_project else 'disabled'}"
        + (f"\nWorkspace:       {resolved_ws}" if resolved_ws else ""),
        title="Budget Status",
        box=box.ROUNDED,
    ))

    if top_consumers:
        t = Table(title="Top 5 Consumers (today)", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Agent Type", style="bold")
        t.add_column("Spend (USD)", justify="right")
        t.add_column("Executions", justify="right")
        t.add_column("Cap", justify="right")
        for c in top_consumers:
            cap_val = agent_caps.get(c["agent_type"])
            cap_str = f"${cap_val:.2f}" if cap_val else "[dim]none[/dim]"
            t.add_row(c["agent_type"], f"${c['spend_usd']:.4f}", str(c["executions"]), cap_str)
        console.print(t)

    if agent_caps:
        t = Table(title="Per-Agent-Type Caps", box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Agent Type", style="bold")
        t.add_column("Daily Cap (USD)", justify="right")
        for agent, cap in sorted(agent_caps.items()):
            t.add_row(agent, f"${cap:.2f}")
        console.print(t)


@budget.command("history")
@click.option("--days", "-d", type=int, default=7, show_default=True, help="Number of days")
@click.option("--workspace", "-w", default=None, help="Filter to workspace")
def budget_history(days: int, workspace: str | None):
    """Show daily spend totals for the last 7 days."""
    from cap.lib.config import load_config
    from cap.lib.harness_config import load_harness_config
    from cap.lib.budget_manager import init_budget_log_table, get_history

    config = load_config()
    harness_cfg = load_harness_config()
    data_dir = config.data_dir
    budget_cfg = harness_cfg.get("budget", {})
    daily_limit = budget_cfg.get("daily_limit_usd", 5.0)
    per_project = budget_cfg.get("per_project", False)

    resolved_ws = _resolve_workspace(workspace) if workspace else None

    try:
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        init_budget_log_table(db)

        ws_filter = resolved_ws if per_project else None
        history = get_history(db, days=days, workspace=ws_filter)
    except Exception as exc:
        console.print(f"[red]Query failed: {exc}[/red]")
        raise SystemExit(1)

    if not history:
        console.print("[dim]No budget history found.[/dim]")
        return

    t = Table(title=f"Budget History (last {days} days)", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Date", style="bold")
    t.add_column("Spend (USD)", justify="right")
    t.add_column("% of Limit", justify="right")
    t.add_column("Executions", justify="right")

    for entry in history:
        spend = entry["total_spend_usd"]
        pct = round(spend / max(daily_limit, 0.01) * 100, 1)
        hist_color = "green" if pct < 60 else ("yellow" if pct < 80 else "red")
        t.add_row(
            entry["date"],
            f"${spend:.4f}",
            f"[{hist_color}]{pct}%[/{hist_color}]",
            str(entry["execution_count"]),
        )

    console.print(t)


@budget.command("pause")
def budget_pause():
    """Pause all budget spending — blocks new executions."""
    from cap.lib.config import load_config
    from cap.lib.budget_manager import pause_budget, is_budget_paused

    if is_budget_paused():
        console.print("[yellow]Budget is already paused.[/yellow]")
        return

    config = load_config()
    data_dir = config.data_dir

    try:
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        pause_budget(db)
        db.close()
    except Exception as exc:
        console.print(f"[red]Pause failed: {exc}[/red]")
        raise SystemExit(1)

    console.print("[red]Budget PAUSED[/red] — all new executions will be blocked.")
    console.print("[dim]Run 'cap budget resume' to resume operations.[/dim]")


@budget.command("resume")
def budget_resume():
    """Resume budget operations — allows executions again."""
    from cap.lib.config import load_config
    from cap.lib.budget_manager import resume_budget, is_budget_paused

    if not is_budget_paused():
        console.print("[yellow]Budget is not paused.[/yellow]")
        return

    config = load_config()
    data_dir = config.data_dir

    try:
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        resume_budget(db)
        db.close()
    except Exception as exc:
        console.print(f"[red]Resume failed: {exc}[/red]")
        raise SystemExit(1)

    console.print("[green]Budget RESUMED[/green] — executions are allowed again.")


@budget.command("reset")
@click.option("--workspace", "-w", default=None, help="Reset specific workspace only")
@click.option("--yes", "confirm", is_flag=True, help="Skip confirmation")
def budget_reset(workspace: str | None, confirm: bool):
    """Reset today's spend counter."""
    from cap.lib.config import load_config
    from cap.lib.budget_manager import reset_today

    if not confirm:
        if not click.confirm("Reset today's budget counter? This cannot be undone."):
            return

    config = load_config()
    data_dir = config.data_dir
    resolved_ws = _resolve_workspace(workspace) if workspace else None

    try:
        db = sqlite3.connect(str(data_dir / "platform.db"))
        db.execute("PRAGMA busy_timeout=2000")
        reset_today(db, workspace=resolved_ws)
        db.close()
    except Exception as exc:
        console.print(f"[red]Reset failed: {exc}[/red]")
        raise SystemExit(1)

    scope = f"workspace {resolved_ws}" if resolved_ws else "all workspaces"
    console.print(f"[green]Budget counter reset[/green] for today ({scope}).")


@budget.command("raise")
@click.argument("amount", type=float)
def budget_raise(amount: float):
    """Raise the daily budget cap (in USD)."""
    from cap.db import get_db, migrate as _migrate

    db_path = os.environ.get("CAP_ORCHESTRATOR_DB", os.path.expanduser("~/.cap/cap.db"))
    db = get_db(db_path)
    _migrate(db)

    # Get current cap
    row = db.execute(
        "SELECT value FROM runtime_state WHERE key = 'daily_budget_usd'"
    ).fetchone()
    current_cap = float(row[0]) if row else 5.0

    new_cap = current_cap + amount
    if new_cap <= 0:
        console.print("[red]Budget cap cannot be zero or negative.[/red]")
        raise SystemExit(1)

    import time as _time
    db.execute(
        "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("daily_budget_usd", str(new_cap), _time.time()),
    )
    db.commit()

    console.print(
        f"[green]Daily budget raised[/green]\n"
        f"  Previous: ${current_cap:.2f}\n"
        f"  New:      ${new_cap:.2f}  (+${amount:.2f})"
    )


# ── cap sync ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--workspace", "-w", default=".", help="Workspace to sync")
@click.option(
    "--trigger",
    type=click.Choice(["session_start", "git_post_pull", "manual"]),
    default="manual",
    show_default=True,
)
@click.option("--full", is_flag=True, help="Force full re-sync (ignore change detection)")
def sync(workspace: str, trigger: str, full: bool):
    """Index workspace files into the knowledge base."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.repo_extractor import extract_and_index_repos
    from cap.lib.sync_engine import sync_workspace

    workspace = _resolve_workspace(workspace)
    config = load_config()
    data_dir = config.data_dir

    console.print(f"[bold]Syncing:[/bold] {workspace}")
    console.print(f"[dim]Mode: {'full re-index' if full else 'incremental'}  Trigger: {trigger}[/dim]\n")

    try:
        db = init_knowledge_db(data_dir)
    except Exception as exc:
        console.print(f"[red]Database init failed: {exc}[/red]")
        raise SystemExit(1)

    # Phase 1: Repo-level summaries (high-quality structured entries)
    console.print("[bold]Phase 1: Repo summaries[/bold]")
    with console.status("[cyan]Extracting repo summaries…[/cyan]"):
        repo_stats = extract_and_index_repos(db, workspace)

    console.print(f"  [green]✓[/green] {repo_stats.repos_found} repos found, "
                  f"{repo_stats.repos_indexed} indexed, "
                  f"{repo_stats.repos_updated} updated, "
                  f"{repo_stats.graph_edges_created} graph edges")
    if repo_stats.errors:
        for err in repo_stats.errors[:3]:
            console.print(f"  [yellow]![/yellow] {err}")

    # Phase 2: File-level indexing (for grep-like precision queries)
    console.print("\n[bold]Phase 2: File indexing[/bold]")
    with console.status("[cyan]Scanning and indexing files…[/cyan]"):
        stats = sync_workspace(db, workspace, full=full)

    table = Table(box=box.SIMPLE)
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Files scanned", str(stats.files_scanned))
    table.add_row("Files indexed (new)", str(stats.files_indexed))
    table.add_row("Files updated", str(stats.files_updated))
    table.add_row("Files unchanged", str(stats.files_unchanged))
    table.add_row("Files skipped", str(stats.files_skipped))
    table.add_row("Graph edges created", str(stats.graph_edges_created))
    table.add_row("Embeddings queued", str(stats.embeddings_queued))
    console.print(table)

    if stats.errors:
        console.print(f"\n[yellow]Warnings ({len(stats.errors)}):[/yellow]")
        for err in stats.errors[:5]:
            console.print(f"  [dim]• {err}[/dim]")
        if len(stats.errors) > 5:
            console.print(f"  [dim]… and {len(stats.errors) - 5} more[/dim]")

    total = stats.files_indexed + stats.files_updated + repo_stats.repos_indexed
    if total > 0:
        console.print(f"\n[green]✓[/green] Knowledge base updated ({total} entries)")
    elif stats.files_unchanged > 0:
        console.print(f"\n[green]✓[/green] Already up to date")
    else:
        console.print("\n[yellow]No indexable content found in workspace[/yellow]")


# ── cap github ─────────────────────────────────────────────────────────────────

@cli.group()
def github():
    """GitHub org configuration and repo resolution."""
    pass


@github.command("config")
@click.option("--org", "-o", default=None, help="GitHub org name (e.g., your-org)")
@click.option("--clone-path", "-p", default=None, help="Base path for cloned repos")
@click.option("--ssh", "use_ssh", is_flag=True, default=None, help="Use SSH protocol")
@click.option("--https", "use_https", is_flag=True, default=None, help="Use HTTPS protocol")
@click.option("--auto-clone", "auto_clone", is_flag=True, default=None, help="Enable auto-clone on missing dep")
@click.option("--no-auto-clone", "no_auto_clone", is_flag=True, default=None, help="Disable auto-clone")
@click.option("--depth", "-d", type=int, default=None, help="Clone depth (0 = full)")
@click.option("--max-clones", type=int, default=None, help="Max auto-clones per session")
@click.option("--show", is_flag=True, help="Show current config")
def github_config(org, clone_path, use_ssh, use_https, auto_clone, no_auto_clone, depth, max_clones, show):
    """Configure GitHub org for auto-resolution of dependent repos."""
    from cap.lib.config import load_config

    config_path = _cap_home() / "config.toml"

    if show:
        cfg = load_config()
        gh = cfg.github
        console.print(Panel(
            f"Org:              [bold cyan]{gh.org or '(not set)'}[/bold cyan]\n"
            f"Clone base path:  {gh.clone_base_path or '(not set)'}\n"
            f"Protocol:         {'SSH' if gh.use_ssh else 'HTTPS'}\n"
            f"Auto-clone:       {'enabled' if gh.auto_clone_on_missing_dep else 'disabled'}\n"
            f"Clone depth:      {gh.clone_depth}\n"
            f"Max per session:  {gh.max_auto_clones_per_session}\n"
            f"Default branch:   {gh.default_branch}\n"
            f"Org URL:          {gh.org_url or '(not set)'}",
            title="GitHub Configuration",
            box=box.ROUNDED,
        ))
        return

    if not any([org, clone_path, use_ssh, use_https, auto_clone, no_auto_clone, depth is not None, max_clones is not None]):
        console.print("[yellow]Provide at least one option to set. Use --show to view current config.[/yellow]")
        return

    try:
        import tomli_w
    except ImportError:
        console.print("[red]tomli-w not installed. Run: pip install tomli-w[/red]")
        raise SystemExit(1)

    try:
        import tomli as tomllib
    except ImportError:
        import tomllib

    existing = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            existing = tomllib.load(f)

    gh_section = existing.get("github", {})
    if org is not None:
        gh_section["org"] = org
    if clone_path is not None:
        gh_section["clone_base_path"] = os.path.abspath(os.path.expanduser(clone_path))
    if use_ssh:
        gh_section["use_ssh"] = True
    elif use_https:
        gh_section["use_ssh"] = False
    if auto_clone:
        gh_section["auto_clone_on_missing_dep"] = True
    elif no_auto_clone:
        gh_section["auto_clone_on_missing_dep"] = False
    if depth is not None:
        gh_section["clone_depth"] = depth
    if max_clones is not None:
        gh_section["max_auto_clones_per_session"] = max_clones

    existing["github"] = gh_section

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "wb") as f:
        tomli_w.dump(existing, f)

    console.print(f"[green]✓[/green] GitHub config saved to {config_path}")
    if org:
        console.print(f"  Org: [cyan]{org}[/cyan]")
    if clone_path:
        console.print(f"  Clone path: [cyan]{gh_section['clone_base_path']}[/cyan]")


@github.command("resolve")
@click.argument("repo_name")
@click.option("--domain", "-d", default=None, help="Domain hint directory")
def github_resolve(repo_name: str, domain: str | None):
    """Resolve a dependent repo (find locally or clone from org)."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.repo_resolver import resolve_repo

    config = load_config()
    data_dir = config.data_dir

    if not config.github.org:
        console.print("[red]GitHub org not configured. Run: cap github config --org YOUR_ORG --clone-path /path[/red]")
        raise SystemExit(1)

    with console.status(f"[cyan]Resolving {repo_name}…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)
            result = resolve_repo(
                repo_name=repo_name,
                db=db,
                config=config.github,
                domain_hint=domain,
            )
        except Exception as exc:
            console.print(f"[red]Resolution failed: {exc}[/red]")
            raise SystemExit(1)

    status = result["status"]
    if status == "found_locally":
        console.print(f"[green]✓[/green] Found locally: {result['path']}")
    elif status == "cloned":
        console.print(f"[green]✓[/green] Cloned: {result['path']}")
        console.print(f"  [dim]Knowledge base updated[/dim]")
    elif status == "not_found_remote":
        console.print(f"[yellow]✗[/yellow] Not found on GitHub: {config.github.org}/{repo_name}")
    else:
        console.print(f"[red]✗[/red] {result.get('message', status)}")


@github.command("deps")
@click.option("--workspace", "-w", default=None, help="Scope to workspace")
@click.option("--auto-clone/--no-auto-clone", "auto_clone", default=True)
@click.option("--max", "max_clones", type=int, default=5, help="Max repos to clone")
def github_deps(workspace: str | None, auto_clone: bool, max_clones: int):
    """Find and resolve unresolved dependencies from knowledge graph."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.repo_resolver import find_unresolved_dependencies, resolve_multiple

    config = load_config()
    data_dir = config.data_dir
    resolved_ws = _resolve_workspace(workspace) if workspace else None

    if not config.github.org:
        console.print("[red]GitHub org not configured. Run: cap github config --org YOUR_ORG --clone-path /path[/red]")
        raise SystemExit(1)

    with console.status("[cyan]Scanning knowledge graph for unresolved deps…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)
            unresolved = find_unresolved_dependencies(db, resolved_ws)
        except Exception as exc:
            console.print(f"[red]Query failed: {exc}[/red]")
            raise SystemExit(1)

    if not unresolved:
        console.print("[green]✓[/green] All dependencies are available locally.")
        return

    t = Table(title=f"Unresolved Dependencies ({len(unresolved)})", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("Repo", style="bold")
    t.add_column("Depended On By")
    t.add_column("Reason")
    for dep in unresolved[:20]:
        t.add_row(dep["repo_name"], dep["depended_on_by"], dep.get("reason", ""))
    console.print(t)

    if auto_clone:
        console.print(f"\n[bold]Auto-cloning up to {max_clones} repos…[/bold]")
        with console.status("[cyan]Cloning…[/cyan]"):
            results = resolve_multiple(
                [dep["repo_name"] for dep in unresolved[:max_clones]],
                db=db,
                config=config.github,
            )

        cloned = sum(1 for r in results if r.get("cloned"))
        console.print(f"[green]✓[/green] Cloned {cloned} / {len(results)} repos")
        for r in results:
            if r.get("cloned"):
                console.print(f"  [green]✓[/green] {r['path']}")
            elif r["status"] != "found_locally":
                console.print(f"  [yellow]✗[/yellow] {r.get('message', '')[:60]}")
    else:
        console.print(f"\n[dim]Run with --auto-clone to resolve these automatically.[/dim]")


# ── cap embed ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--batch-size", "-b", default=100, help="Entries per batch")
@click.option("--max-entries", "-n", default=0, help="Max entries to process (0 = all)")
@click.option("--profile", "-p", default=None, help="AWS profile for Bedrock access")
def embed(batch_size: int, max_entries: int, profile: str):
    """Generate embeddings for queued knowledge entries via Bedrock Titan."""
    import asyncio
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.embeddings import EmbeddingClient, EmbeddingConfig

    config = load_config()
    data_dir = config.data_dir

    try:
        db = init_knowledge_db(data_dir)
    except Exception as exc:
        console.print(f"[red]Database init failed: {exc}[/red]")
        raise SystemExit(1)

    # Count pending
    pending_count = db.execute(
        "SELECT COUNT(*) FROM embedding_queue WHERE status = 'pending'"
    ).fetchone()[0]

    if pending_count == 0:
        console.print("[green]✓[/green] No pending embeddings — queue is empty.")
        return

    limit = max_entries if max_entries > 0 else pending_count
    console.print(f"[bold]Embedding:[/bold] {min(limit, pending_count)} of {pending_count} pending entries")
    console.print(f"[dim]Model: amazon.titan-embed-text-v2:0  Region: {config.bedrock.region}[/dim]\n")

    # Init embedding client
    embed_config = EmbeddingConfig(
        region=config.bedrock.region,
        profile=profile or config.bedrock.profile,
        max_concurrent=config.bedrock.embedding_max_concurrent,
    )
    client = EmbeddingClient(embed_config)

    # Init LanceDB
    vectors_dir = data_dir / "vectors"
    vectors_dir.mkdir(parents=True, exist_ok=True)

    try:
        import lancedb
        import pyarrow as pa

        lance_db = lancedb.connect(str(vectors_dir))
        try:
            vectors_table = lance_db.open_table("knowledge_vectors")
        except Exception:
            schema = pa.schema([
                pa.field("uuid", pa.string()),
                pa.field("workspace", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 1024)),
            ])
            vectors_table = lance_db.create_table("knowledge_vectors", schema=schema)
    except ImportError:
        console.print("[red]LanceDB not available — install with: pip install lancedb[/red]")
        raise SystemExit(1)

    # Process in batches
    processed = 0
    succeeded = 0
    failed = 0

    async def _process_all():
        nonlocal processed, succeeded, failed

        async_client = EmbeddingClient(embed_config)

        while processed < limit:
            batch_limit = min(batch_size, limit - processed)
            rows = db.execute(
                """SELECT eq.entry_id, ke.uuid, ke.content
                   FROM embedding_queue eq
                   JOIN knowledge_entries ke ON ke.id = eq.entry_id
                   WHERE eq.status = 'pending'
                   LIMIT ?""",
                (batch_limit,),
            ).fetchall()

            if not rows:
                break

            entries = [(r[0], r[1], r[2] or "") for r in rows]
            texts = [content for _, _, content in entries]
            vectors = await async_client.embed_batch(texts)

            for (entry_id, uuid, _), vector in zip(entries, vectors):
                if vector is not None:
                    workspace = db.execute(
                        "SELECT workspace FROM knowledge_entries WHERE id = ?", (entry_id,)
                    ).fetchone()[0]
                    vectors_table.add([{
                        "uuid": uuid,
                        "workspace": workspace,
                        "vector": vector,
                    }])
                    db.execute(
                        "UPDATE knowledge_entries SET embedding_status = 'embedded' WHERE id = ?",
                        (entry_id,),
                    )
                    db.execute(
                        "UPDATE embedding_queue SET status = 'done' WHERE entry_id = ?",
                        (entry_id,),
                    )
                    succeeded += 1
                else:
                    db.execute(
                        "UPDATE embedding_queue SET status = 'failed' WHERE entry_id = ?",
                        (entry_id,),
                    )
                    failed += 1
            db.commit()
            processed += len(entries)

        return async_client

    with console.status("[cyan]Generating embeddings via Bedrock…[/cyan]"):
        client = asyncio.run(_process_all())

    console.print(f"\n[green]✓[/green] Processed: {processed}  Succeeded: {succeeded}  Failed: {failed}")

    if client.is_available is False:
        console.print("[yellow]⚠ Bedrock unavailable — check AWS credentials/region[/yellow]")


# ── cap eval ──────────────────────────────────────────────────────────────────

@cli.group()
def eval():
    """Run quality evaluations."""
    pass


@eval.command("run")
@click.option("--suite", "-s", "suite_name", default=None, help="Run specific suite (retrieval, session, security, workflow)")
@click.option("--output", "-o", "output_path", default=None, help="Export JSON report to file")
@click.option("--verbose", "-v", is_flag=True, help="Show individual case results")
def eval_run(suite_name: str | None, output_path: str | None, verbose: bool):
    """Run evaluation suites."""
    from cap.eval.framework import EvalSuite
    from cap.eval.suites import ALL_SUITES

    suites_to_run = {}
    if suite_name:
        if suite_name not in ALL_SUITES:
            console.print(f"[red]Unknown suite: {suite_name}[/red]")
            console.print(f"[dim]Available: {', '.join(ALL_SUITES.keys())}[/dim]")
            raise SystemExit(1)
        suites_to_run = {suite_name: ALL_SUITES[suite_name]}
    else:
        suites_to_run = ALL_SUITES

    all_reports = []
    for name, suite_cls in suites_to_run.items():
        console.print(f"\n[bold cyan]Running: {name}[/bold cyan]")
        suite = suite_cls()
        with console.status(f"[cyan]Evaluating {name}...[/cyan]"):
            report = suite.run()
        all_reports.append(report)

        # Summary
        color = "green" if report.pass_rate >= 0.8 else ("yellow" if report.pass_rate >= 0.5 else "red")
        console.print(
            f"  [{color}]{report.passed}/{report.total_cases} passed[/{color}] "
            f"({report.pass_rate*100:.0f}%) │ "
            f"Score: [{color}]{report.overall_score:.2f}[/{color}] │ "
            f"Time: {report.duration_ms:.0f}ms"
        )

        if verbose and report.worst_performers:
            console.print("  [dim]Worst performers:[/dim]")
            for wp in report.worst_performers[:3]:
                console.print(f"    [red]✗[/red] {wp.case.name}: {wp.score:.3f} (need {wp.case.threshold})")

        if report.recommendations:
            console.print("  [dim]Recommendations:[/dim]")
            for rec in report.recommendations[:2]:
                console.print(f"    → {rec}")

    if output_path:
        import json as _json
        combined = {
            "reports": [r.to_dict() for r in all_reports],
            "summary": {
                "total_suites": len(all_reports),
                "total_cases": sum(r.total_cases for r in all_reports),
                "total_passed": sum(r.passed for r in all_reports),
                "overall_pass_rate": sum(r.passed for r in all_reports) / max(sum(r.total_cases for r in all_reports), 1),
            }
        }
        Path(output_path).write_text(_json.dumps(combined, indent=2, default=str))
        console.print(f"\n[green]Report exported:[/green] {output_path}")


@eval.command("list")
def eval_list():
    """List available evaluation suites."""
    from cap.eval.suites import ALL_SUITES

    table = Table(title="Evaluation Suites", box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Suite", style="cyan bold")
    table.add_column("Description")
    table.add_column("Cases", justify="right")

    for name, suite_cls in ALL_SUITES.items():
        suite = suite_cls()
        cases = suite.get_cases()
        desc = suite.__class__.__doc__ or ""
        table.add_row(name, desc.strip().split("\n")[0], str(len(cases)))

    console.print(table)
    console.print(f"\n[dim]Run all: cap eval run[/dim]")
    console.print(f"[dim]Run one: cap eval run --suite retrieval[/dim]")


@eval.command("report")
@click.argument("file_path")
def eval_report(file_path: str):
    """Display a saved evaluation report."""
    path = Path(file_path)
    if not path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        raise SystemExit(1)

    import json as _json
    data = _json.loads(path.read_text())

    if "reports" in data:
        for report_data in data["reports"]:
            console.print(Panel(
                f"Suite: [bold]{report_data['suite_name']}[/bold]\n"
                f"Time: {report_data['timestamp']}\n"
                f"Cases: {report_data['total_cases']} │ "
                f"Passed: [green]{report_data['passed']}[/green] │ "
                f"Failed: [red]{report_data['failed']}[/red]\n"
                f"Score: [bold]{report_data['overall_score']:.3f}[/bold] │ "
                f"Pass Rate: {report_data['pass_rate']*100:.0f}%",
                title="Eval Report",
                box=box.ROUNDED,
            ))

            if report_data.get("categories"):
                t = Table(title="By Category", box=box.SIMPLE_HEAD, show_edge=False)
                t.add_column("Category", style="bold")
                t.add_column("Pass Rate", justify="right")
                t.add_column("Avg Score", justify="right")
                t.add_column("p95 Latency", justify="right")
                for cat in report_data["categories"]:
                    t.add_row(
                        cat["category"],
                        f"{cat['pass_rate']*100:.0f}%",
                        f"{cat['avg_score']:.3f}",
                        f"{cat['p95_latency_ms']:.0f}ms",
                    )
                console.print(t)
    else:
        console.print(f"[yellow]Unrecognized report format.[/yellow]")


# ── cap backlog ───────────────────────────────────────────────────────────────

@cli.group()
def backlog():
    """Persistent task backlog management."""
    pass


@backlog.command("list")
@click.option(
    "--status", "-s",
    type=click.Choice(["backlog", "ready", "in_progress", "in_review", "blocked", "done", "cancelled"]),
    default=None,
)
@click.option("--assigned", "-a", default=None, help="Filter by assigned agent")
@click.option("--limit", "-n", type=int, default=50)
def backlog_list(status: str | None, assigned: str | None, limit: int):
    """List backlog tasks."""
    from cap.lib.backlog import init_backlog_table, list_tasks, TaskStatus
    from cap.lib.db_init import _open_existing, create_database

    db_path = _data_dir() / "backlog.db"
    if not db_path.exists():
        console.print("[dim]No backlog database found.[/dim]")
        return

    db = _open_existing(db_path)
    init_backlog_table(db)
    ts = TaskStatus(status) if status else None
    tasks = list_tasks(db, status=ts, assigned_to=assigned, limit=limit)

    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    t = Table(title=f"Backlog ({len(tasks)} tasks)", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("ID", style="dim", width=12)
    t.add_column("P", width=2)
    t.add_column("Status", width=12)
    t.add_column("Title", max_width=40)
    t.add_column("Assigned", width=14)
    t.add_column("Criteria", justify="right", width=8)

    priority_icons = {"critical": "[red]![/red]", "high": "[yellow]H[/yellow]", "medium": "M", "low": "[dim]L[/dim]"}
    for task in tasks:
        met = sum(1 for c in task.acceptance_criteria if c.verified)
        total = len(task.acceptance_criteria)
        crit_str = f"{met}/{total}" if total else "—"
        t.add_row(
            task.id[:12],
            priority_icons.get(task.priority.value, "?"),
            _status_color(task.status.value),
            task.title[:40],
            task.assigned_to[:14] if task.assigned_to else "—",
            crit_str,
        )
    console.print(t)


@backlog.command("stats")
def backlog_stats_cmd():
    """Show backlog statistics."""
    from cap.lib.backlog import init_backlog_table, backlog_stats
    from cap.lib.db_init import _open_existing

    db_path = _data_dir() / "backlog.db"
    if not db_path.exists():
        console.print("[dim]No backlog database found.[/dim]")
        return

    db = _open_existing(db_path)
    init_backlog_table(db)
    stats = backlog_stats(db)

    console.print(Panel(
        f"Total tasks:     [cyan]{stats['total']}[/cyan]\n"
        f"Completion:      [green]{stats['completion_pct']}%[/green]\n"
        f"In progress:     [yellow]{stats['in_progress']}[/yellow]\n"
        f"Blocked:         [{'red' if stats['blocked'] else 'dim'}]{stats['blocked']}[/{'red' if stats['blocked'] else 'dim'}]\n"
        f"By status:       {json.dumps(stats['by_status'], indent=2)}",
        title="Backlog Stats",
        box=box.ROUNDED,
    ))


# ── cap decisions ─────────────────────────────────────────────────────────────

@cli.group()
def decisions():
    """Decision card management."""
    pass


@decisions.command("list")
@click.option(
    "--status", "-s",
    type=click.Choice(["pending", "approved", "rejected", "deferred"]),
    default=None,
)
@click.option("--limit", "-n", type=int, default=20)
def decisions_list(status: str | None, limit: int):
    """List decision cards."""
    from cap.lib.decision_cards import init_decision_cards_table, list_cards, DecisionStatus
    from cap.lib.db_init import _open_existing

    db_path = _data_dir() / "backlog.db"
    if not db_path.exists():
        console.print("[dim]No backlog database found.[/dim]")
        return

    db = _open_existing(db_path)
    init_decision_cards_table(db)
    ds = DecisionStatus(status) if status else None
    cards = list_cards(db, status=ds, limit=limit)

    if not cards:
        console.print("[dim]No decision cards found.[/dim]")
        return

    t = Table(title=f"Decisions ({len(cards)})", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("ID", style="dim", width=12)
    t.add_column("Status", width=10)
    t.add_column("Title", max_width=40)
    t.add_column("Options", justify="right", width=7)
    t.add_column("Domain", width=12)
    t.add_column("Created")

    for card in cards:
        t.add_row(
            card.id[:12],
            _status_color(card.status.value),
            card.title[:40],
            str(len(card.options)),
            card.domain or "—",
            (card.created_at or "")[:10],
        )
    console.print(t)


# ── cap conflicts ─────────────────────────────────────────────────────────────

@cli.group()
def conflicts():
    """Inter-agent disagreement management."""
    pass


@conflicts.command("list")
@click.option(
    "--status", "-s",
    type=click.Choice(["open", "escalated", "resolved", "overridden"]),
    default=None,
)
@click.option("--limit", "-n", type=int, default=20)
def conflicts_list(status: str | None, limit: int):
    """List conflicts."""
    from cap.lib.disagreement import init_conflicts_table, list_conflicts, ConflictStatus
    from cap.lib.db_init import _open_existing

    db_path = _data_dir() / "backlog.db"
    if not db_path.exists():
        console.print("[dim]No backlog database found.[/dim]")
        return

    db = _open_existing(db_path)
    init_conflicts_table(db)
    cs = ConflictStatus(status) if status else None
    items = list_conflicts(db, status=cs, limit=limit)

    if not items:
        console.print("[dim]No conflicts found.[/dim]")
        return

    t = Table(title=f"Conflicts ({len(items)})", box=box.SIMPLE_HEAD, show_edge=False)
    t.add_column("ID", style="dim", width=12)
    t.add_column("Severity", width=10)
    t.add_column("Status", width=10)
    t.add_column("Title", max_width=40)
    t.add_column("Created")

    sev_colors = {"advisory": "dim", "warning": "yellow", "blocking": "red"}
    for item in items:
        sev_color = sev_colors.get(item.severity.value, "white")
        t.add_row(
            item.id[:12],
            f"[{sev_color}]{item.severity.value}[/{sev_color}]",
            _status_color(item.status.value),
            item.title[:40],
            (item.created_at or "")[:10],
        )
    console.print(t)


# ── cap dashboard ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--poll", "-p", type=float, default=2.0, help="Refresh interval in seconds")
@click.option("--once", is_flag=True, help="Render once and exit (no live refresh)")
def dashboard(poll: float, once: bool):
    """Real-time status dashboard (TUI)."""
    console.print("[red]dashboard is not available: dashboard module removed[/red]")
    raise SystemExit(1)


# ── cap git ───────────────────────────────────────────────────────────────────

@cli.group("git")
def git_cmd():
    """Git knowledge ingestion."""
    pass


@git_cmd.command("ingest")
@click.option("--workspace", "-w", default=".", help="Repo path")
@click.option("--max-prs", type=int, default=30, help="Max PRs to fetch")
@click.option("--files", "-f", multiple=True, help="Key files for blame analysis")
def git_ingest(workspace: str, max_prs: int, files: tuple):
    """Ingest git blame + PR discussions into knowledge base."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db
    from cap.lib.git_knowledge import ingest_git_knowledge

    workspace = os.path.abspath(os.path.expanduser(workspace))
    config = load_config()
    data_dir = config.data_dir

    # Auto-discover key files if none specified
    key_files = list(files) if files else None
    if not key_files:
        from pathlib import Path as _Path
        repo = _Path(workspace)
        patterns = ["*.tf", "*.py", "*.go", "*.ts", "Dockerfile", "*.yaml"]
        discovered = []
        for pattern in patterns:
            for f in repo.rglob(pattern):
                if ".git" not in str(f) and "node_modules" not in str(f):
                    discovered.append(str(f.relative_to(repo)))
                    if len(discovered) >= 50:
                        break
            if len(discovered) >= 50:
                break
        key_files = discovered[:50]

    with console.status(f"[cyan]Ingesting git knowledge from {workspace}…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)
            stats = ingest_git_knowledge(
                repo_path=workspace,
                knowledge_db=db,
                workspace=workspace,
                max_prs=max_prs,
                key_files=key_files,
            )
        except Exception as exc:
            console.print(f"[red]Ingestion failed: {exc}[/red]")
            raise SystemExit(1)

    console.print(
        f"[green]✓[/green] Ingested: "
        f"{stats['ownership_entries']} ownership entries, "
        f"{stats['pr_entries']} PR discussions"
    )
    if stats["errors"]:
        console.print(f"  [yellow]{stats['errors']} errors[/yellow]")


# ── cap drift ─────────────────────────────────────────────────────────────────

@cli.group()
def drift():
    """Terraform drift detection."""
    pass


@drift.command("check")
@click.argument("workspace", default=".")
@click.option("--profile", "-p", default="", help="AWS profile for terraform")
@click.option("--auto-task/--no-auto-task", default=True, help="Create backlog task on drift")
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON")
def drift_check(workspace: str, profile: str, auto_task: bool, json_out: bool):
    """Check a terraform workspace for drift."""
    from cap.lib.drift_sentinel import check_drift_and_report

    workspace = os.path.abspath(os.path.expanduser(workspace))
    backlog_db = str(_data_dir() / "backlog.db") if auto_task else None

    with console.status(f"[cyan]Running terraform plan on {workspace}…[/cyan]"):
        report = check_drift_and_report(workspace, profile, backlog_db)

    if json_out:
        console.print_json(json.dumps(report.to_dict()))
        return

    if report.has_drift:
        console.print(f"[red]DRIFT DETECTED[/red] in {workspace}")
        console.print(f"  {report.summary}")
        t = Table(box=box.SIMPLE_HEAD, show_edge=False)
        t.add_column("Resource", style="bold")
        t.add_column("Change Type")
        for f in report.findings:
            color = {"create": "green", "delete": "red", "update": "yellow", "replace": "magenta"}.get(f.change_type, "white")
            t.add_row(f.resource_address, f"[{color}]{f.change_type}[/{color}]")
        console.print(t)
        if auto_task:
            console.print("[dim]Backlog task created automatically.[/dim]")
    elif report.plan_exit_code == 0:
        console.print(f"[green]No drift[/green] in {workspace} ({report.duration_seconds:.1f}s)")
    else:
        console.print(f"[red]Error[/red] (exit {report.plan_exit_code}): {report.plan_stderr[:200]}")


# ── cap passthrough ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--workspace", "-w", default=".", help="Workspace path")
@click.option("--ttl", "-t", type=int, default=300, show_default=True, help="Time-to-live in seconds")
@click.option("--reason", "-r", default="", help="Reason for enabling passthrough")
@click.option("--disable", "do_disable", is_flag=True, help="Disable passthrough immediately")
@click.option("--check", "do_check", is_flag=True, help="Check if passthrough is active")
def passthrough(workspace: str, ttl: int, reason: str, do_disable: bool, do_check: bool):
    """Temporarily bypass enforcement (max 5 min, max 3/hour)."""
    from cap.enforcement.passthrough import enable as pt_enable, check as pt_check, disable as pt_disable

    workspace = _resolve_workspace(workspace)

    if do_check:
        active = pt_check(workspace)
        if active:
            console.print(f"[green]Passthrough ACTIVE[/green] for {workspace}")
        else:
            console.print(f"[dim]Passthrough inactive for {workspace}[/dim]")
        return

    if do_disable:
        pt_disable(workspace)
        console.print(f"[yellow]Passthrough disabled[/yellow] for {workspace}")
        return

    result = pt_enable(workspace, ttl=ttl, reason=reason)
    if "error" in result:
        console.print(f"[red]{result['error']}[/red]")
        raise SystemExit(1)

    console.print(
        f"[green]Passthrough enabled[/green] for {workspace}\n"
        f"  Expires in: [cyan]{result['expires_in']}s[/cyan]\n"
        + (f"  Reason: {result['reason']}\n" if result.get('reason') else "")
        + f"  [dim]Enforcement is paused. Max 3 activations per hour.[/dim]"
    )


# ── cap daemon ────────────────────────────────────────────────────────────────

@cli.group("daemon", invoke_without_command=True)
@click.pass_context
def daemon_group(ctx):
    """CAP background daemon management."""
    if ctx.invoked_subcommand is None:
        # Default: show status
        ctx.invoke(daemon_status_cmd)


@daemon_group.command("status")
def daemon_status_cmd():
    """Show daemon status: PID, uptime, server count, last health check."""
    from cap.harness.daemon import read_pid, is_daemon_running, _pid_path, _log_path

    pid = read_pid()
    running = is_daemon_running()

    if not running:
        if pid:
            console.print(f"[yellow]Daemon is NOT running[/yellow] (stale PID {pid})")
        else:
            console.print("[dim]Daemon is not running.[/dim]")
        console.print(f"[dim]Start with: cap daemon start[/dim]")
        return

    # Read uptime from PID file mtime
    pid_path = _pid_path()
    uptime_str = "unknown"
    if pid_path.exists():
        import time as _time
        started = pid_path.stat().st_mtime
        elapsed = _time.time() - started
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        uptime_str = f"{hours}h {minutes}m"

    console.print(Panel(
        f"[bold]PID:[/bold]              {pid}\n"
        f"[bold]Status:[/bold]           [green]running[/green]\n"
        f"[bold]Uptime:[/bold]           {uptime_str}\n"
        f"[bold]PID file:[/bold]         {pid_path}\n"
        f"[bold]Log file:[/bold]         {_log_path()}",
        title="CAP Daemon",
        box=box.ROUNDED,
    ))


@daemon_group.command("start")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)")
@click.option("--once", is_flag=True, help="Run all tasks once and exit")
def daemon_start_cmd(foreground: bool, once: bool):
    """Start the daemon if not already running."""
    import json as _json
    import subprocess
    from cap.harness.daemon import CapDaemon, is_daemon_running, read_pid

    if once:
        d = CapDaemon()
        results = d.run_once()
        click.echo(_json.dumps(results, indent=2, default=str))
        return

    if is_daemon_running():
        pid = read_pid()
        console.print(f"[yellow]Daemon is already running (PID {pid})[/yellow]")
        return

    if foreground:
        console.print("[bold]Starting daemon in foreground...[/bold] (Ctrl+C to stop)")
        from cap.harness.daemon import main as daemon_main
        daemon_main()
    else:
        # Start as background process
        import sys as _sys
        cmd = [_sys.executable, "-m", "cap.harness.daemon"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give it a moment to write PID
        import time as _time
        _time.sleep(1)

        if proc.poll() is None:
            console.print(f"[green]Daemon started[/green] (PID {proc.pid})")
        else:
            console.print(f"[red]Daemon failed to start[/red] (exit code {proc.returncode})")
            raise SystemExit(1)


@daemon_group.command("stop")
def daemon_stop_cmd():
    """Stop the running daemon (SIGTERM + wait)."""
    import time as _time
    from cap.harness.daemon import read_pid, is_daemon_running, remove_pid

    pid = read_pid()
    if not pid or not is_daemon_running():
        console.print("[dim]Daemon is not running.[/dim]")
        if pid:
            remove_pid()
        return

    # Send SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        console.print(f"[red]Failed to stop daemon: {e}[/red]")
        remove_pid()
        return

    # Wait for process to exit (max 10s)
    console.print(f"[dim]Sending SIGTERM to PID {pid}...[/dim]")
    for _ in range(20):
        try:
            os.kill(pid, 0)
            _time.sleep(0.5)
        except ProcessLookupError:
            break
    else:
        # Force kill
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    remove_pid()
    console.print(f"[green]Daemon stopped[/green] (was PID {pid})")


@daemon_group.command("restart")
@click.pass_context
def daemon_restart_cmd(ctx):
    """Restart the daemon (stop + start)."""
    ctx.invoke(daemon_stop_cmd)
    import time as _time
    _time.sleep(1)
    ctx.invoke(daemon_start_cmd, foreground=False, once=False)


@daemon_group.command("logs")
@click.option("--lines", "-n", type=int, default=50, show_default=True, help="Number of lines to show")
@click.option("--follow", "-f", "follow_flag", is_flag=True, help="Follow log output")
def daemon_logs_cmd(lines: int, follow_flag: bool):
    """Tail the daemon log file."""
    import subprocess
    from cap.harness.daemon import _log_path

    log_file = _log_path()
    if not log_file.exists():
        console.print(f"[dim]No log file found at {log_file}[/dim]")
        return

    if follow_flag:
        console.print(f"[dim]Following {log_file} (Ctrl+C to stop)...[/dim]")
        try:
            subprocess.run(["tail", "-f", "-n", str(lines), str(log_file)])
        except KeyboardInterrupt:
            pass
    else:
        try:
            result = subprocess.run(
                ["tail", "-n", str(lines), str(log_file)],
                capture_output=True, text=True,
            )
            if result.stdout:
                console.print(result.stdout)
            else:
                console.print("[dim]Log file is empty.[/dim]")
        except Exception as exc:
            console.print(f"[red]Failed to read log: {exc}[/red]")


@daemon_group.command("install")
def daemon_install_cmd():
    """Install daemon as OS service (LaunchAgent/systemd)."""
    from cap.cli.daemon_service import install_service, is_service_installed

    if is_service_installed():
        console.print("[yellow]Service is already installed.[/yellow]")
        return

    try:
        path = install_service()
        console.print(f"[green]Service installed:[/green] {path}")
        console.print("[dim]The daemon will start on login and auto-restart on crash.[/dim]")
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)


@daemon_group.command("uninstall")
def daemon_uninstall_cmd():
    """Uninstall the daemon OS service."""
    from cap.cli.daemon_service import uninstall_service, is_service_installed

    if not is_service_installed():
        console.print("[dim]Service is not installed.[/dim]")
        return

    removed = uninstall_service()
    if removed:
        console.print("[green]Service uninstalled.[/green]")
    else:
        console.print("[yellow]Could not uninstall service.[/yellow]")


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()

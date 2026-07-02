"""cap index — Intelligent indexing CLI commands.

Provides:
  cap index run       — trigger full/incremental indexing pipeline
  cap index status    — show indexing state
  cap index deps      — show resolved dependencies
  cap index graph     — query the knowledge graph
  cap index daemon    — configure daemon re-indexing
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

console = Console(stderr=True)

from cap.config import get_data_dir as _get_data_dir
_STATE_FILE = _get_data_dir() / "indexer_state.json"
_DAEMON_CONFIG_FILE = _get_data_dir() / "indexer_daemon.json"


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _resolve_workspace(workspace: str) -> str:
    """Resolve and normalize a workspace path."""
    return os.path.abspath(os.path.expanduser(workspace))


def _load_state() -> dict[str, Any]:
    """Load indexer state from the state file."""
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _load_daemon_config() -> dict[str, Any]:
    """Load daemon configuration from the daemon config file."""
    if not _DAEMON_CONFIG_FILE.exists():
        return {"enabled": False, "interval_minutes": 60}
    try:
        return json.loads(_DAEMON_CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"enabled": False, "interval_minutes": 60}


def _save_daemon_config(cfg: dict[str, Any]) -> None:
    """Persist daemon configuration to disk."""
    _DAEMON_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DAEMON_CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def _format_cost(cost: float) -> str:
    """Format a cost value for display."""
    if cost < 0.001:
        return f"${cost:.6f}"
    if cost < 1.0:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


# ── Root group ──────────────────────────────────────────────────────────────────

@click.group("index")
def index_group():
    """Intelligent code indexing and knowledge graph."""
    pass


# ── cap index run ───────────────────────────────────────────────────────────────

@index_group.command("run")
@click.option("--workspace", "-w", default=".", show_default=True, help="Path to index")
@click.option("--budget", "-b", type=float, default=2.0, show_default=True, help="Max USD budget")
@click.option("--skip-llm", "skip_llm", is_flag=True, help="Skip LLM analysis phase")
@click.option("--skip-embeddings", "skip_embeddings", is_flag=True, help="Skip embedding generation")
@click.option("--full", "force_full", is_flag=True, help="Force full re-index (ignore incremental state)")
@click.option("--concurrency", "-c", type=int, default=3, show_default=True, help="Max parallel LLM calls")
def index_run(
    workspace: str,
    budget: float,
    skip_llm: bool,
    skip_embeddings: bool,
    force_full: bool,
    concurrency: int,
) -> None:
    """Trigger the full indexing pipeline for a workspace."""
    from cap.lib.intelligent_indexer import IntelligentIndexer, IndexerConfig, IndexRunResult
    from cap.lib.config import load_config

    workspace = _resolve_workspace(workspace)
    cap_config = load_config()
    data_dir = cap_config.data_dir

    mode = "full re-index" if force_full else "incremental"
    phases = []
    if not skip_llm:
        phases.append("LLM analysis")
    if not skip_embeddings:
        phases.append("embeddings")
    phases_str = ", ".join(phases) if phases else "file scan only"

    console.print(Panel(
        f"[bold]Workspace:[/bold]  {workspace}\n"
        f"[bold]Mode:[/bold]       {mode}\n"
        f"[bold]Budget:[/bold]     {_format_cost(budget)}\n"
        f"[bold]Phases:[/bold]     {phases_str}\n"
        f"[bold]Concurrency:[/bold] {concurrency}",
        title="Index Run",
        box=box.ROUNDED,
    ))

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    phase_task: TaskID | None = None
    _progress_ref: dict[str, Any] = {}

    def progress_callback(phase: str, detail: dict[str, Any]) -> None:
        """Update Rich progress display as indexer phases advance."""
        nonlocal phase_task

        total = detail.get("total", 100)
        current = detail.get("current", 0)
        description = detail.get("description", phase)

        if phase_task is None:
            phase_task = progress.add_task(description, total=total)
            _progress_ref["task"] = phase_task
        else:
            progress.update(phase_task, description=description, total=total, completed=current)

    indexer_config = IndexerConfig(
        workspace=workspace,
        data_dir=data_dir,
        budget_usd=budget,
        skip_llm=skip_llm,
        skip_embeddings=skip_embeddings,
        force_full=force_full,
        max_concurrency=concurrency,
    )
    indexer = IntelligentIndexer(config=indexer_config, progress_callback=progress_callback)

    async def _run() -> IndexRunResult:
        return await indexer.run()

    result: IndexRunResult | None = None
    try:
        with progress:
            phase_task = progress.add_task("Starting…", total=100)
            result = asyncio.run(_run())
            progress.update(phase_task, completed=100, description="Complete")
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        raise SystemExit(1)
    except Exception as exc:
        console.print(f"\n[red]Error: Indexing pipeline failed — {exc}[/red]")
        raise SystemExit(1)

    if result is None:
        console.print("[red]Error: No result returned from indexer.[/red]")
        raise SystemExit(1)

    # Summary table
    summary = Table(title="Index Run Summary", box=box.SIMPLE_HEAD, show_edge=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")

    summary.add_row("Repos discovered", str(result.repos_discovered))
    summary.add_row("Repos indexed", str(result.repos_indexed))
    summary.add_row("Files processed", str(result.files_processed))
    summary.add_row("Graph nodes created", str(result.graph_nodes_created))
    summary.add_row("Graph edges created", str(result.graph_edges_created))
    summary.add_row("Embeddings generated", str(result.embeddings_generated))
    summary.add_row("LLM calls made", str(result.llm_calls_made))
    summary.add_row("Cost", _format_cost(result.cost_usd))
    summary.add_row("Duration", f"{result.duration_seconds:.1f}s")

    cost_color = "red" if result.cost_usd >= budget * 0.9 else ("yellow" if result.cost_usd >= budget * 0.6 else "green")
    console.print(summary)
    console.print(
        f"\n[{cost_color}]Cost: {_format_cost(result.cost_usd)} / {_format_cost(budget)} budget[/{cost_color}]"
    )

    if result.errors:
        console.print(f"\n[yellow]Warnings ({len(result.errors)}):[/yellow]")
        for err in result.errors[:5]:
            console.print(f"  [dim]• {err}[/dim]")
        if len(result.errors) > 5:
            console.print(f"  [dim]… and {len(result.errors) - 5} more[/dim]")

    console.print(f"\n[green]Done.[/green] Knowledge graph updated for {workspace}")


# ── cap index status ─────────────────────────────────────────────────────────────

@index_group.command("status")
def index_status() -> None:
    """Show indexing state, coverage, and budget remaining."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db

    cap_config = load_config()
    data_dir = cap_config.data_dir
    state = _load_state()

    last_run = state.get("last_run_at", "never")
    repos_indexed = state.get("repos_indexed", 0)
    repos_discovered = state.get("repos_discovered", 0)
    last_cost = state.get("last_run_cost_usd", 0.0)
    budget_used = state.get("cumulative_cost_usd", 0.0)
    budget_limit = state.get("budget_limit_usd", 2.0)
    budget_remaining = max(budget_limit - budget_used, 0.0)

    coverage_pct = round(repos_indexed / max(repos_discovered, 1) * 100, 1)
    cov_color = "green" if coverage_pct >= 80 else ("yellow" if coverage_pct >= 50 else "red")
    budget_pct = round(budget_used / max(budget_limit, 0.01) * 100, 1)
    bud_color = "green" if budget_pct < 60 else ("yellow" if budget_pct < 85 else "red")

    # Query live stats from knowledge db
    live_nodes = 0
    live_edges = 0
    live_entries = 0
    try:
        db = init_knowledge_db(data_dir)
        live_entries = db.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        live_nodes = db.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone()[0]
        live_edges = db.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone()[0]
    except Exception:
        pass

    console.print(Panel(
        f"[bold]Last run:[/bold]         {last_run}\n"
        f"[bold]Repos indexed:[/bold]    {repos_indexed} / {repos_discovered} "
        f"([{cov_color}]{coverage_pct}% coverage[/{cov_color}])\n"
        f"[bold]Last run cost:[/bold]    {_format_cost(last_cost)}\n"
        f"[bold]Budget used:[/bold]      {_format_cost(budget_used)} / {_format_cost(budget_limit)} "
        f"([{bud_color}]{budget_pct}%[/{bud_color}])\n"
        f"[bold]Budget remaining:[/bold] [{bud_color}]{_format_cost(budget_remaining)}[/{bud_color}]\n"
        f"[bold]Knowledge entries:[/bold] {live_entries}\n"
        f"[bold]Graph nodes:[/bold]      {live_nodes}\n"
        f"[bold]Graph edges:[/bold]      {live_edges}",
        title="Indexer Status",
        box=box.ROUNDED,
    ))

    if not state:
        console.print("[dim]No indexer state found. Run `cap index run` to build the index.[/dim]")


# ── cap index deps ───────────────────────────────────────────────────────────────

@index_group.command("deps")
@click.option("--repo", "-r", "repo_filter", default=None, help="Filter to a specific source repo")
@click.option(
    "--type", "-t", "dep_type",
    type=click.Choice(["terraform", "helm", "argocd", "python", "go"]),
    default=None,
    help="Filter by dependency type",
)
def index_deps(repo_filter: str | None, dep_type: str | None) -> None:
    """Show resolved dependencies from the knowledge graph."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db

    cap_config = load_config()
    data_dir = cap_config.data_dir

    with console.status("[cyan]Querying knowledge graph…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)

            query = (
                "SELECT e.source_id, ns.title as source_name, e.target_id, nt.title as target_name, "
                "       e.metadata "
                "FROM knowledge_graph_edges e "
                "LEFT JOIN knowledge_graph_nodes ns ON ns.id = e.source_id "
                "LEFT JOIN knowledge_graph_nodes nt ON nt.id = e.target_id "
                "WHERE e.predicate = 'depends-on'"
            )
            params: list[Any] = []

            if repo_filter:
                query += " AND (ns.title LIKE ? OR ns.id LIKE ?)"
                pattern = f"%{repo_filter}%"
                params.extend([pattern, pattern])

            query += " ORDER BY ns.title, nt.title LIMIT 500"

            rows = db.execute(query, params).fetchall()
        except Exception as exc:
            console.print(f"[red]Error: Failed to query dependencies — {exc}[/red]")
            raise SystemExit(1)

    if not rows:
        msg = "No dependencies found"
        if repo_filter:
            msg += f" for repo '{repo_filter}'"
        if dep_type:
            msg += f" of type '{dep_type}'"
        console.print(f"[dim]{msg}.[/dim]")
        return

    # Parse metadata and optionally filter by dep_type
    filtered: list[tuple[str, str, str, str]] = []
    for source_id, source_name, target_id, target_name, raw_meta in rows:
        meta: dict[str, Any] = {}
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                pass

        edge_dep_type = meta.get("dep_type", meta.get("type", "unknown"))
        reference = meta.get("reference", meta.get("version", ""))

        if dep_type and edge_dep_type != dep_type:
            continue

        filtered.append((
            source_name or source_id or "—",
            target_name or target_id or "—",
            edge_dep_type,
            reference,
        ))

    if not filtered:
        console.print(f"[dim]No dependencies of type '{dep_type}' found.[/dim]")
        return

    title_parts = ["Dependencies"]
    if repo_filter:
        title_parts.append(f"repo={repo_filter}")
    if dep_type:
        title_parts.append(f"type={dep_type}")

    t = Table(
        title="  ".join(title_parts) + f"  ({len(filtered)})",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )
    t.add_column("Source Repo", style="bold", max_width=35)
    t.add_column("Target", max_width=35)
    t.add_column("Type", width=12)
    t.add_column("Reference", max_width=30)

    for source, target, dtype, ref in filtered:
        t.add_row(source, target, dtype, ref or "[dim]—[/dim]")

    console.print(t)


# ── cap index graph ──────────────────────────────────────────────────────────────

@index_group.command("graph")
@click.option("--node", "-n", "node_filter", default=None, help="Find a specific node by name or ID")
@click.option("--connected", "show_connected", is_flag=True, help="Show connected nodes for --node")
@click.option("--domain", "-d", "domain_filter", default=None, help="Filter nodes by domain")
@click.option("--tag", "tag_filter", default=None, help="Filter nodes by tag")
@click.option("--stats", "show_stats", is_flag=True, help="Show graph statistics")
@click.option(
    "--export",
    "export_format",
    type=click.Choice(["dot", "mermaid"]),
    default=None,
    help="Export graph as dot or mermaid",
)
def index_graph(
    node_filter: str | None,
    show_connected: bool,
    domain_filter: str | None,
    tag_filter: str | None,
    show_stats: bool,
    export_format: str | None,
) -> None:
    """Query and explore the knowledge graph."""
    from cap.lib.config import load_config
    from cap.lib.db_init import init_knowledge_db

    cap_config = load_config()
    data_dir = cap_config.data_dir

    with console.status("[cyan]Querying knowledge graph…[/cyan]"):
        try:
            db = init_knowledge_db(data_dir)

            if show_stats:
                total_nodes = db.execute("SELECT COUNT(*) FROM knowledge_graph_nodes").fetchone()[0]
                total_edges = db.execute("SELECT COUNT(*) FROM knowledge_graph_edges").fetchone()[0]

                by_type = db.execute(
                    "SELECT node_type, COUNT(*) as cnt FROM knowledge_graph_nodes "
                    "GROUP BY node_type ORDER BY cnt DESC LIMIT 20"
                ).fetchall()

                by_predicate = db.execute(
                    "SELECT predicate, COUNT(*) as cnt FROM knowledge_graph_edges "
                    "GROUP BY predicate ORDER BY cnt DESC LIMIT 20"
                ).fetchall()

                console.print(Panel(
                    f"[bold]Nodes:[/bold] {total_nodes}\n"
                    f"[bold]Edges:[/bold] {total_edges}",
                    title="Knowledge Graph Statistics",
                    box=box.ROUNDED,
                ))

                if by_type:
                    nt = Table(title="Nodes by Type", box=box.SIMPLE_HEAD, show_edge=False)
                    nt.add_column("Type", style="bold")
                    nt.add_column("Count", justify="right")
                    for row in by_type:
                        nt.add_row(row[0] or "unknown", str(row[1]))
                    console.print(nt)

                if by_predicate:
                    pt = Table(title="Edges by Predicate", box=box.SIMPLE_HEAD, show_edge=False)
                    pt.add_column("Predicate", style="bold")
                    pt.add_column("Count", justify="right")
                    for row in by_predicate:
                        pt.add_row(row[0] or "unknown", str(row[1]))
                    console.print(pt)
                return

            # Node lookup
            node_where = "WHERE 1=1"
            node_params: list[Any] = []

            if node_filter:
                node_where += " AND (n.id LIKE ? OR n.title LIKE ?)"
                pattern = f"%{node_filter}%"
                node_params.extend([pattern, pattern])

            if domain_filter:
                node_where += " AND n.domain = ?"
                node_params.append(domain_filter)

            if tag_filter:
                node_where += " AND n.tags LIKE ?"
                node_params.append(f"%{tag_filter}%")

            nodes = db.execute(
                f"SELECT n.id, n.title, n.node_type, n.domain, n.tags, n.workspace "
                f"FROM knowledge_graph_nodes n {node_where} "
                f"ORDER BY n.title LIMIT 100",
                node_params,
            ).fetchall()

        except Exception as exc:
            console.print(f"[red]Error: Failed to query knowledge graph — {exc}[/red]")
            raise SystemExit(1)

    if not nodes:
        console.print("[dim]No nodes found matching the given filters.[/dim]")
        return

    if export_format:
        _export_graph(db, nodes, export_format)
        return

    title_parts = ["Knowledge Graph Nodes"]
    if node_filter:
        title_parts.append(f"name~{node_filter}")
    if domain_filter:
        title_parts.append(f"domain={domain_filter}")
    if tag_filter:
        title_parts.append(f"tag={tag_filter}")

    nt = Table(
        title="  ".join(title_parts) + f"  ({len(nodes)})",
        box=box.SIMPLE_HEAD,
        show_edge=False,
    )
    nt.add_column("Title", style="bold", max_width=40)
    nt.add_column("Type", width=14)
    nt.add_column("Domain", width=16)
    nt.add_column("Tags", max_width=30)
    nt.add_column("ID", style="dim", max_width=20)

    for node_id, title, node_type, domain, tags, _ws in nodes:
        nt.add_row(
            title or "—",
            node_type or "—",
            domain or "—",
            (tags or "")[:30],
            (node_id or "")[:20],
        )

    console.print(nt)

    if show_connected and node_filter and len(nodes) == 1:
        target_id = nodes[0][0]
        _show_connected_nodes(db, target_id, nodes[0][1] or target_id)


def _show_connected_nodes(db: Any, node_id: str, node_title: str) -> None:
    """Print all nodes connected to node_id via any edge."""
    try:
        outbound = db.execute(
            "SELECT e.predicate, n.title, n.node_type, e.target_id "
            "FROM knowledge_graph_edges e "
            "LEFT JOIN knowledge_graph_nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? LIMIT 50",
            (node_id,),
        ).fetchall()

        inbound = db.execute(
            "SELECT e.predicate, n.title, n.node_type, e.source_id "
            "FROM knowledge_graph_edges e "
            "LEFT JOIN knowledge_graph_nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? LIMIT 50",
            (node_id,),
        ).fetchall()
    except Exception as exc:
        console.print(f"[red]Error: Failed to fetch connected nodes — {exc}[/red]")
        return

    if outbound:
        ot = Table(title=f"Outbound from '{node_title}'", box=box.SIMPLE_HEAD, show_edge=False)
        ot.add_column("Predicate", style="bold cyan", width=18)
        ot.add_column("Target", max_width=40)
        ot.add_column("Type", width=14)
        for predicate, title, ntype, nid in outbound:
            ot.add_row(predicate or "—", title or nid or "—", ntype or "—")
        console.print(ot)

    if inbound:
        it = Table(title=f"Inbound to '{node_title}'", box=box.SIMPLE_HEAD, show_edge=False)
        it.add_column("Predicate", style="bold magenta", width=18)
        it.add_column("Source", max_width=40)
        it.add_column("Type", width=14)
        for predicate, title, ntype, nid in inbound:
            it.add_row(predicate or "—", title or nid or "—", ntype or "—")
        console.print(it)

    if not outbound and not inbound:
        console.print("[dim]No connected nodes found.[/dim]")


def _export_graph(db: Any, nodes: list[tuple], fmt: str) -> None:
    """Export nodes and their edges as dot or mermaid to stdout."""
    node_ids = [n[0] for n in nodes]
    placeholders = ",".join("?" * len(node_ids))

    try:
        edges = db.execute(
            f"SELECT source_id, predicate, target_id FROM knowledge_graph_edges "
            f"WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            node_ids + node_ids,
        ).fetchall()
    except Exception as exc:
        console.print(f"[red]Error: Export query failed — {exc}[/red]")
        raise SystemExit(1)

    id_to_title: dict[str, str] = {n[0]: (n[1] or n[0]) for n in nodes}

    if fmt == "dot":
        lines = ["digraph knowledge_graph {", "  rankdir=LR;"]
        for node_id, title, *_ in nodes:
            safe = title.replace('"', '\\"') if title else node_id
            lines.append(f'  "{node_id}" [label="{safe}"];')
        for src, pred, tgt in edges:
            lines.append(f'  "{src}" -> "{tgt}" [label="{pred}"];')
        lines.append("}")
        click.echo("\n".join(lines))

    elif fmt == "mermaid":
        lines = ["graph LR"]
        for node_id, title, *_ in nodes:
            safe = (title or node_id).replace('"', "'")
            short_id = node_id.replace("-", "_")[:30]
            lines.append(f'  {short_id}["{safe}"]')
        for src, pred, tgt in edges:
            src_s = src.replace("-", "_")[:30]
            tgt_s = tgt.replace("-", "_")[:30]
            lines.append(f"  {src_s} -->|{pred}| {tgt_s}")
        click.echo("\n".join(lines))


# ── cap index daemon ─────────────────────────────────────────────────────────────

@index_group.command("daemon")
@click.option("--interval", "-i", "interval_minutes", type=int, default=None, help="Re-index interval in minutes")
@click.option("--enable/--disable", "enabled", default=None, help="Enable or disable daemon re-indexing")
@click.option("--status", "show_status", is_flag=True, help="Show daemon configuration")
def index_daemon(interval_minutes: int | None, enabled: bool | None, show_status: bool) -> None:
    """Configure daemon-mode re-indexing."""
    cfg = _load_daemon_config()

    if show_status or (interval_minutes is None and enabled is None):
        daemon_enabled = cfg.get("enabled", False)
        daemon_interval = cfg.get("interval_minutes", 60)
        last_daemon_run = cfg.get("last_run_at", "never")
        next_run = cfg.get("next_run_at", "unknown")

        state_color = "green" if daemon_enabled else "dim"
        console.print(Panel(
            f"[bold]State:[/bold]           [{state_color}]{'enabled' if daemon_enabled else 'disabled'}[/{state_color}]\n"
            f"[bold]Interval:[/bold]        {daemon_interval} minutes\n"
            f"[bold]Last run:[/bold]        {last_daemon_run}\n"
            f"[bold]Next run:[/bold]        {next_run}\n"
            f"[bold]Config file:[/bold]     {_DAEMON_CONFIG_FILE}",
            title="Indexer Daemon Configuration",
            box=box.ROUNDED,
        ))
        return

    changed = False

    if interval_minutes is not None:
        if interval_minutes < 1:
            console.print("[red]Error: Interval must be at least 1 minute.[/red]")
            raise SystemExit(1)
        cfg["interval_minutes"] = interval_minutes
        changed = True
        console.print(f"[green]✓[/green] Interval set to {interval_minutes} minutes")

    if enabled is not None:
        cfg["enabled"] = enabled
        changed = True
        state_word = "enabled" if enabled else "disabled"
        state_color = "green" if enabled else "yellow"
        console.print(f"[{state_color}]✓[/{state_color}] Daemon re-indexing {state_word}")

    if changed:
        try:
            _save_daemon_config(cfg)
        except OSError as exc:
            console.print(f"[red]Error: Failed to save daemon config — {exc}[/red]")
            raise SystemExit(1)
        console.print(f"[dim]Config saved to {_DAEMON_CONFIG_FILE}[/dim]")

        if cfg.get("enabled"):
            console.print(
                f"[dim]Daemon will re-index every {cfg['interval_minutes']} minutes "
                f"when the CAP harness is running.[/dim]"
            )

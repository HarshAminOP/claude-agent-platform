"""cap workflow watch — watch a running workflow as team conversation."""

import sys
import time
from pathlib import Path

import click
from rich.console import Console

from cap.lib.config import load_config
from cap.lib.workflow_observer import WorkflowObserver


@click.command("watch")
@click.argument("workflow_id", required=False)
@click.option("--poll", "-p", type=float, default=2.0, help="Poll interval in seconds")
def watch(workflow_id, poll):
    """Watch a workflow as team conversation. If no ID given, watches the latest running workflow."""
    console = Console(stderr=True)
    config = load_config()
    db_path = config.data_dir / "platform.db"

    if not db_path.exists():
        console.print("[red]No platform database found. Run `cap install` first.[/red]")
        sys.exit(1)

    import sqlite3
    conn = sqlite3.connect(str(db_path))

    if not workflow_id:
        row = conn.execute(
            "SELECT id, name FROM workflows WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id, name FROM workflows ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            console.print("[yellow]No workflows found.[/yellow]")
            sys.exit(0)
        workflow_id = row[0]
        console.print(f"[dim]Watching: {row[1]} ({workflow_id})[/dim]")

    conn.close()

    observer = WorkflowObserver(db_path, workflow_id)
    try:
        observer.watch(poll_interval=poll)
    except KeyboardInterrupt:
        observer.stop()
        console.print("\n[dim]Stopped watching.[/dim]")

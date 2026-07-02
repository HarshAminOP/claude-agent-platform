"""CAP CLI — Witness manifest commands.

Provides:
- cap witness status: show table of witnessed files (file, hash, reviewer, status)
- cap witness --accept-risk: force-accept stale/failed witness stamps
"""

import os
import sqlite3
import time

import click
from rich.console import Console
from rich.table import Table
from rich import box

console = Console(stderr=True)


def _get_db() -> sqlite3.Connection:
    """Get the CAP database connection with witness tables."""
    from cap.config import get_platform_db_path
    from cap.db import get_db, migrate
    db_path = os.environ.get("CAP_ORCHESTRATOR_DB", str(get_platform_db_path()))
    db = get_db(db_path)
    migrate(db)
    return db


@click.group("witness")
def witness():
    """Witness manifest — cryptographic file review proofs."""
    pass


@witness.command("status")
@click.option("--workspace", "-w", default=".", help="Filter to workspace directory")
@click.option("--limit", "-n", type=int, default=50, show_default=True)
def witness_status(workspace: str, limit: int):
    """Show witnessed files with hash validity status."""
    from cap.integrity.witness import WitnessManifest

    db = _get_db()

    # Query all witness entries (optionally filtered by workspace prefix)
    resolved_ws = os.path.abspath(os.path.expanduser(workspace))

    rows = db.execute(
        """SELECT file_path, content_hash, reviewer, workflow_id, stamped_at, verified_at
           FROM witness_manifests
           ORDER BY stamped_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        console.print("[dim]No witness stamps found.[/dim]")
        return

    # Filter to workspace if not "."
    if workspace != ".":
        rows = [r for r in rows if str(r[0]).startswith(resolved_ws)]

    if not rows:
        console.print(f"[dim]No witness stamps found for {resolved_ws}[/dim]")
        return

    table = Table(title=f"Witness Manifest ({len(rows)} entries)", box=box.SIMPLE_HEAD, show_edge=False)
    table.add_column("File", max_width=50)
    table.add_column("Hash", width=12, style="dim")
    table.add_column("Reviewer", width=16)
    table.add_column("Status")

    import hashlib

    for row in rows:
        if isinstance(row, (tuple, list)):
            file_path, content_hash, reviewer, workflow_id, stamped_at, verified_at = row
        else:
            file_path = row["file_path"]
            content_hash = row["content_hash"]
            reviewer = row["reviewer"]
            workflow_id = row["workflow_id"]
            stamped_at = row["stamped_at"]
            verified_at = row["verified_at"]

        # Check current file state
        if not os.path.isfile(file_path):
            status = "[red]missing[/red]"
        else:
            try:
                h = hashlib.sha256()
                with open(file_path, "rb") as f:
                    while chunk := f.read(65536):
                        h.update(chunk)
                current_hash = h.hexdigest()
                if current_hash == content_hash:
                    status = "[green]valid[/green]"
                else:
                    status = "[yellow]stale[/yellow]"
            except OSError:
                status = "[red]error[/red]"

        # Shorten display
        display_path = file_path
        if len(display_path) > 50:
            display_path = "..." + display_path[-47:]

        table.add_row(
            display_path,
            content_hash[:12],
            reviewer or "--",
            status,
        )

    console.print(table)


@witness.command("accept-risk")
@click.option("--workspace", "-w", default=".", help="Workspace directory")
@click.confirmation_option(prompt="Force-accept stale witness stamps? This overrides review protection.")
def witness_accept_risk(workspace: str):
    """Force-accept stale witness stamps (re-stamp files at current hash)."""
    from cap.integrity.witness import WitnessManifest

    db = _get_db()
    resolved_ws = os.path.abspath(os.path.expanduser(workspace))

    import hashlib

    # Find stale entries (hash mismatch)
    rows = db.execute(
        "SELECT file_path, content_hash, reviewer, workflow_id FROM witness_manifests"
    ).fetchall()

    stale_files = []
    for row in rows:
        if isinstance(row, (tuple, list)):
            file_path, stored_hash, reviewer, workflow_id = row
        else:
            file_path = row["file_path"]
            stored_hash = row["content_hash"]
            reviewer = row["reviewer"]
            workflow_id = row["workflow_id"]

        # Filter to workspace
        if not file_path.startswith(resolved_ws):
            continue

        if not os.path.isfile(file_path):
            continue

        try:
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            current_hash = h.hexdigest()
            if current_hash != stored_hash:
                stale_files.append((file_path, current_hash, reviewer, workflow_id))
        except OSError:
            continue

    if not stale_files:
        console.print("[green]No stale witness stamps found — all valid.[/green]")
        return

    # Re-stamp at current hash
    now = time.time()
    for file_path, current_hash, reviewer, workflow_id in stale_files:
        db.execute(
            """INSERT OR REPLACE INTO witness_manifests
               (file_path, content_hash, reviewer, workflow_id, stamped_at)
               VALUES (?, ?, ?, ?, ?)""",
            (file_path, current_hash, f"risk-accept:{reviewer}", workflow_id, now),
        )
    db.commit()

    console.print(f"[yellow]Force-accepted {len(stale_files)} stale witness stamp(s).[/yellow]")
    for file_path, _, _, _ in stale_files[:10]:
        display = file_path if len(file_path) <= 60 else "..." + file_path[-57:]
        console.print(f"  [dim]{display}[/dim]")
    if len(stale_files) > 10:
        console.print(f"  [dim]... and {len(stale_files) - 10} more[/dim]")

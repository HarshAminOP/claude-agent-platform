"""cap workflow daemon — auto-watches new workflows as team conversations.

Polls platform.db for new workflows entering 'running' state and spawns
a WorkflowObserver for each. Designed to run in background during a Claude
Code session.

Usage:
    cap workflow daemon          # foreground (Ctrl-C to stop)
    cap workflow daemon --bg     # daemonize to background
"""

import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import click
from rich.console import Console

from cap.lib.config import load_config
from cap.lib.workflow_observer import WorkflowObserver

console = Console(stderr=True)


class WorkflowDaemon:
    """Watches for new workflows and auto-renders them as team conversations."""

    def __init__(self, db_path: Path, poll_interval: float = 3.0):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._known_workflows: set[str] = set()
        self._active_observers: dict[str, WorkflowObserver] = {}
        self._threads: dict[str, threading.Thread] = {}

    def _load_existing(self):
        """Load already-known workflow IDs so we don't re-watch old ones."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute("SELECT id FROM workflows").fetchall()
        conn.close()
        self._known_workflows = {r[0] for r in rows}

    def _check_new_workflows(self):
        """Check for newly started workflows."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT id, name FROM workflows WHERE status = 'running'"
        ).fetchall()
        conn.close()

        for wf_id, wf_name in rows:
            if wf_id not in self._known_workflows:
                self._known_workflows.add(wf_id)
                self._start_observer(wf_id, wf_name)

    def _start_observer(self, workflow_id: str, workflow_name: str):
        """Spawn an observer thread for a new workflow."""
        console.print(f"\n[bold cyan]New workflow detected:[/bold cyan] {workflow_name} ({workflow_id[:16]})")

        observer = WorkflowObserver(self.db_path, workflow_id)
        self._active_observers[workflow_id] = observer

        thread = threading.Thread(
            target=self._run_observer,
            args=(workflow_id, observer),
            daemon=True,
        )
        self._threads[workflow_id] = thread
        thread.start()

    def _run_observer(self, workflow_id: str, observer: WorkflowObserver):
        """Run an observer until it completes."""
        try:
            observer.watch(poll_interval=self.poll_interval)
        except Exception as e:
            console.print(f"[red]Observer error ({workflow_id[:12]}): {e}[/red]")
        finally:
            self._active_observers.pop(workflow_id, None)
            self._threads.pop(workflow_id, None)

    def _cleanup_finished(self):
        """Remove references to finished observer threads."""
        dead = [wf_id for wf_id, t in self._threads.items() if not t.is_alive()]
        for wf_id in dead:
            self._threads.pop(wf_id, None)
            self._active_observers.pop(wf_id, None)

    def run(self):
        """Main loop — poll for new workflows until stopped."""
        self._load_existing()
        console.print("[dim]Workflow daemon started. Watching for new workflows...[/dim]")

        while not self._stop.is_set():
            try:
                self._check_new_workflows()
                self._cleanup_finished()
            except Exception as e:
                console.print(f"[yellow]Poll error: {e}[/yellow]")

            self._stop.wait(self.poll_interval)

        # Wait for active observers to finish
        for t in list(self._threads.values()):
            t.join(timeout=5)

        console.print("[dim]Daemon stopped.[/dim]")

    def stop(self):
        """Stop the daemon and all observers."""
        self._stop.set()
        for obs in list(self._active_observers.values()):
            obs.stop()


@click.command("daemon")
@click.option("--poll", "-p", type=float, default=3.0, show_default=True, help="Poll interval seconds")
@click.option("--bg", is_flag=True, help="Run in background (daemonize)")
def daemon(poll: float, bg: bool):
    """Auto-watch new workflows as team conversations."""
    config = load_config()
    db_path = config.data_dir / "platform.db"

    if not db_path.exists():
        console.print("[red]Platform database not found. Run `cap doctor` first.[/red]")
        raise SystemExit(1)

    if bg:
        pid = os.fork()
        if pid > 0:
            console.print(f"[dim]Daemon started (PID {pid}). Watching for workflows.[/dim]")
            return
        # Child process
        os.setsid()
        sys.stdin.close()

    d = WorkflowDaemon(db_path, poll_interval=poll)

    def _handle_signal(signum, frame):
        d.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    d.run()

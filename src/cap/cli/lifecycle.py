"""cap init / cap uninstall — lifecycle management.

Handles first-time setup and clean removal of all CAP artifacts.
Designed for `uv tool install claude-agent-platform` distribution model.

CRITICAL: Before modifying ANY user config file (~/.claude.json, ~/.claude/settings.json),
we create a timestamped backup in ~/.claude-platform/backups/. On uninstall, the original
configs are restored automatically. This ensures CAP never breaks a user's existing setup.

After install:
    cap init              # creates ~/.claude-platform, registers MCP servers, installs agents
    cap init --minimal    # just databases + config, no agent/workflow install

Before removal:
    cap uninstall         # deregisters MCP servers, removes ~/.claude-platform, restores configs
    cap uninstall --keep-data   # removes config but keeps databases
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.resources import files as pkg_files
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(stderr=True)


# ── Path helpers ──────────────────────────────────────────────────────────────

def _cap_home() -> Path:
    return Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))


def _claude_dir() -> Path:
    return Path.home() / ".claude"


def _claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _settings_json_path() -> Path:
    return _claude_dir() / "settings.json"


def _backups_dir() -> Path:
    return _cap_home() / "backups"


def _run_claude_mcp(args: list[str]) -> bool:
    """Run `claude mcp ...` command. Returns True on success or already-exists."""
    try:
        result = subprocess.run(
            ["claude", "mcp"] + args,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True
        if "already exists" in result.stderr.lower() or "already exists" in result.stdout.lower():
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_bundled_data_path() -> Path:
    """Get path to bundled data within the package."""
    return Path(str(pkg_files("cap.data")))


# ── Config Backup/Restore ─────────────────────────────────────────────────────

def _backup_file(file_path: Path, label: str) -> Path | None:
    """Create a timestamped backup of a file. Returns backup path or None if file doesn't exist."""
    if not file_path.exists():
        return None

    backups = _backups_dir()
    backups.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    backup_name = f"{label}.backup.{timestamp}"
    backup_path = backups / backup_name

    shutil.copy2(file_path, backup_path)
    os.chmod(backup_path, 0o600)
    return backup_path


def _get_latest_backup(label: str) -> Path | None:
    """Find the most recent backup for a given label."""
    backups = _backups_dir()
    if not backups.exists():
        return None

    candidates = sorted(
        [f for f in backups.iterdir() if f.name.startswith(f"{label}.backup.")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _restore_file(label: str, target_path: Path) -> bool:
    """Restore a file from its latest backup. Returns True if restored."""
    backup = _get_latest_backup(label)
    if not backup:
        return False

    shutil.copy2(backup, target_path)
    return True


def _backup_user_configs() -> dict[str, str]:
    """Backup all user config files that CAP might modify. Returns {label: backup_path}."""
    backups_made = {}

    configs_to_backup = [
        (_claude_json_path(), "claude-json"),
        (_settings_json_path(), "settings-json"),
    ]

    for file_path, label in configs_to_backup:
        backup_path = _backup_file(file_path, label)
        if backup_path:
            backups_made[label] = str(backup_path)

    return backups_made


def _restore_user_configs() -> dict[str, bool]:
    """Restore all user configs from backup. Returns {label: success}."""
    results = {}

    restores = [
        ("claude-json", _claude_json_path()),
        ("settings-json", _settings_json_path()),
    ]

    for label, target in restores:
        results[label] = _restore_file(label, target)

    return results


# ── CLAUDE.md Instructions ────────────────────────────────────────────────────

_CAP_INSTRUCTIONS_START = "<!-- CAP:START — Auto-managed by Claude Agent Platform. Do not edit this block. -->"
_CAP_INSTRUCTIONS_END = "<!-- CAP:END -->"

_CAP_INSTRUCTIONS_BODY = """
## CAP — Claude Agent Platform (MCP Tools)

You have access to CAP MCP servers that provide persistent knowledge, session memory, and workflow coordination.

### Information Retrieval Priority

When answering questions about the codebase, repos, architecture, or past decisions:

1. **Use `knowledge_search` FIRST** — it searches the indexed knowledge base (FTS5 + graph). Faster and more complete than grep.
2. **Use `knowledge_graph_query`** — to traverse relationships between services, modules, and resources.
3. **Use `session_recall`** — to find past decisions, learnings, and corrections.
4. **Use bash/grep ONLY** — when you need exact file contents not yet indexed, or to read/modify specific files.

### Session Memory

At the start of complex work:
- Use `session_start` to begin a tracked session
- Use `session_record` to save decisions, learnings, or corrections during work
- Use `session_feedback` when the user corrects you — this persists across sessions

### Workflow Coordination

For multi-specialist tasks, use the workflow engine:
- `workflow_start` — kick off a multi-agent workflow
- `workflow_status` — check progress
- `workflow_kill` — abort a runaway workflow

### Budget Awareness

Workflows are budget-constrained. The workflow engine tracks token usage and kills workflows that exceed limits. Check budget with `workflow_estimate` before starting expensive operations.

### What NOT to do

- Do NOT use bash grep/find to search for information when `knowledge_search` can answer it
- Do NOT re-discover repo structure manually — the knowledge graph already has it indexed
- Do NOT ignore session corrections — they persist specifically so you won't repeat mistakes
"""


_LEGACY_INFO_HIERARCHY_MARKERS = [
    "## Information Hierarchy (STRICT ORDER)",
    "## Information Hierarchy",
]


def _install_claude_instructions(claude_dir: Path, force: bool = False) -> bool:
    """Append CAP instructions to ~/.claude/CLAUDE.md. Returns True if modified."""
    claude_md = claude_dir / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text()
        if _CAP_INSTRUCTIONS_START in content:
            if not force:
                return False
            content = _remove_cap_instructions(content)
        content = _replace_legacy_info_hierarchy(content)
    else:
        claude_dir.mkdir(parents=True, exist_ok=True)
        content = ""

    block = f"\n{_CAP_INSTRUCTIONS_START}\n{_CAP_INSTRUCTIONS_BODY}\n{_CAP_INSTRUCTIONS_END}\n"
    content = content.rstrip() + "\n" + block
    claude_md.write_text(content)
    return True


def _replace_legacy_info_hierarchy(content: str) -> str:
    """Replace legacy file-based info hierarchy with a pointer to CAP MCP tools."""
    for marker in _LEGACY_INFO_HIERARCHY_MARKERS:
        idx = content.find(marker)
        if idx == -1:
            continue
        next_section = content.find("\n## ", idx + len(marker))
        if next_section == -1:
            end_idx = len(content)
        else:
            end_idx = next_section
        replacement = (
            "## Information Hierarchy (STRICT ORDER)\n\n"
            "When agents need to understand something:\n"
            "1. **CAP `knowledge_search` FIRST** — searches the indexed knowledge base (FTS5 + semantic + graph). Covers all repos.\n"
            "2. **CAP `session_recall`** — for past decisions, learnings, and corrections.\n"
            "3. **MCP Servers** — Use structured tools (aws-iam, aws-eks, kubernetes, terraform, aws-cloudwatch) for live state.\n"
            "4. **Bash LAST** — Only for exact file contents not yet indexed, or for execution (tests, builds).\n\n"
            "When spawning agents: use `knowledge_search` to gather context, not bash grep.\n"
        )
        content = content[:idx] + replacement + content[end_idx:]
        break
    return content


def _remove_cap_instructions(content: str) -> str:
    """Remove the CAP instructions block from CLAUDE.md content."""
    start_idx = content.find(_CAP_INSTRUCTIONS_START)
    end_idx = content.find(_CAP_INSTRUCTIONS_END)
    if start_idx == -1 or end_idx == -1:
        return content
    return content[:start_idx].rstrip() + content[end_idx + len(_CAP_INSTRUCTIONS_END):]


def _uninstall_claude_instructions(claude_dir: Path) -> bool:
    """Remove CAP instructions from ~/.claude/CLAUDE.md. Returns True if modified."""
    claude_md = claude_dir / "CLAUDE.md"
    if not claude_md.exists():
        return False

    content = claude_md.read_text()
    if _CAP_INSTRUCTIONS_START not in content:
        return False

    new_content = _remove_cap_instructions(content)
    claude_md.write_text(new_content)
    return True


# ── Manifest ──────────────────────────────────────────────────────────────────

MANIFEST_FILE = "cap-manifest.json"


def _load_manifest(cap_home: Path) -> dict:
    mf = cap_home / MANIFEST_FILE
    if mf.exists():
        return json.loads(mf.read_text())
    return {
        "version": "0.3.0",
        "installed_agents": [],
        "installed_workflows": [],
        "mcp_servers": [],
        "backups": {},
    }


def _save_manifest(cap_home: Path, manifest: dict):
    mf = cap_home / MANIFEST_FILE
    mf.write_text(json.dumps(manifest, indent=2) + "\n")


# ── cap init ──────────────────────────────────────────────────────────────────

@click.command()
@click.option("--minimal", is_flag=True, help="Only create databases + config (no agents/workflows)")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--skip-mcp", is_flag=True, help="Don't register MCP servers with Claude")
def init(minimal: bool, force: bool, skip_mcp: bool):
    """Initialize the CAP platform (run after install)."""
    from cap.lib.db_init import initialize_all_databases

    cap_home = _cap_home()
    data_dir = cap_home / "data"
    claude_dir = _claude_dir()

    console.print(Panel(
        f"[bold]CAP — Claude Agent Platform[/bold] v0.3.0\n"
        f"Home: {cap_home}",
        box=box.ROUNDED, style="cyan",
    ))

    manifest = _load_manifest(cap_home)

    # ── 1. Create directories ─────────────────────────────────────────────
    console.print("\n[bold]1. Creating directories[/bold]")
    for d in [cap_home, data_dir, cap_home / "logs", _backups_dir()]:
        d.mkdir(parents=True, exist_ok=True)
    console.print(f"  [green]✓[/green] {cap_home}")

    # ── 2. Backup existing user configs ───────────────────────────────────
    console.print("\n[bold]2. Backing up user configs[/bold]")
    backups = _backup_user_configs()
    if backups:
        for label, path in backups.items():
            console.print(f"  [green]✓[/green] {label} → {Path(path).name}")
        manifest["backups"] = backups
    else:
        console.print(f"  [dim]─[/dim] No existing configs to backup")

    # ── 3. Config ─────────────────────────────────────────────────────────
    console.print("\n[bold]3. Platform configuration[/bold]")
    config_path = cap_home / "config.toml"
    bundled = _get_bundled_data_path()

    if not config_path.exists() or force:
        src = bundled / "config.toml.default"
        if src.exists():
            shutil.copy2(src, config_path)
            console.print(f"  [green]✓[/green] config.toml created")
        else:
            console.print(f"  [yellow]![/yellow] Bundled config not found — creating minimal")
            config_path.write_text('[platform]\nversion = "0.3.0"\nlog_level = "INFO"\n')
    else:
        console.print(f"  [dim]─[/dim] config.toml exists (use --force to overwrite)")

    # ── 4. Databases ──────────────────────────────────────────────────────
    console.print("\n[bold]4. Initializing databases[/bold]")
    initialize_all_databases(data_dir)
    for db_name in ["platform.db", "knowledge.db", "sessions.db", "fleet.db"]:
        db_path = data_dir / db_name
        os.chmod(db_path, 0o600)
    console.print(f"  [green]✓[/green] 4 databases initialized (WAL mode, 0600 permissions)")

    # ── 5. Agents ─────────────────────────────────────────────────────────
    if not minimal:
        console.print("\n[bold]5. Installing agents[/bold]")
        agents_dir = claude_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        agents_src = bundled / "agents"
        installed_agents = []
        if agents_src.exists():
            for agent_file in sorted(agents_src.iterdir()):
                if agent_file.suffix == ".md":
                    dest = agents_dir / agent_file.name
                    if not dest.exists() or force:
                        shutil.copy2(agent_file, dest)
                        installed_agents.append(agent_file.stem)
            console.print(f"  [green]✓[/green] {len(installed_agents)} agents → {agents_dir}")
        else:
            console.print(f"  [yellow]![/yellow] No bundled agents found")
        manifest["installed_agents"] = installed_agents
    else:
        console.print("\n[bold]5. Agents[/bold] — skipped (--minimal)")

    # ── 6. Workflows ──────────────────────────────────────────────────────
    if not minimal:
        console.print("\n[bold]6. Installing workflows[/bold]")
        workflows_dir = claude_dir / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)

        workflows_src = bundled / "workflows"
        installed_workflows = []
        if workflows_src.exists():
            for wf_file in sorted(workflows_src.iterdir()):
                if wf_file.suffix == ".js":
                    dest = workflows_dir / wf_file.name
                    if not dest.exists() or force:
                        shutil.copy2(wf_file, dest)
                        installed_workflows.append(wf_file.stem)
            console.print(f"  [green]✓[/green] {len(installed_workflows)} workflows → {workflows_dir}")
        else:
            console.print(f"  [yellow]![/yellow] No bundled workflows found")
        manifest["installed_workflows"] = installed_workflows
    else:
        console.print("\n[bold]6. Workflows[/bold] — skipped (--minimal)")

    # ── 7. MCP Servers ────────────────────────────────────────────────────
    if not skip_mcp:
        console.print("\n[bold]7. Registering MCP servers[/bold]")

        python_bin = sys.executable
        servers_dir = Path(__file__).parent.parent / "servers"

        mcp_servers = [
            ("cap-knowledge", str(servers_dir / "knowledge_server.py"),
             [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"]),
            ("cap-session", str(servers_dir / "session_server.py"),
             [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"]),
            ("cap-fleet", str(servers_dir / "fleet_server.py"),
             [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"]),
            ("workflow-engine", str(servers_dir / "workflow_server.py"),
             [f"PLATFORM_DATA_DIR={data_dir}", f"PYTHONPATH={cap_home}"]),
        ]

        registered = []
        for name, script, env_vars in mcp_servers:
            if force:
                _run_claude_mcp(["remove", "--scope", "user", name])
            env_args = []
            for var in env_vars:
                env_args.extend(["-e", var])
            ok = _run_claude_mcp(["add", name, "--scope", "user"] + env_args + ["--", python_bin, script])
            if ok:
                console.print(f"  [green]✓[/green] {name}")
                registered.append(name)
            else:
                console.print(f"  [yellow]![/yellow] {name} — failed (register manually with `claude mcp add`)")
        manifest["mcp_servers"] = registered
    else:
        console.print("\n[bold]7. MCP servers[/bold] — skipped (--skip-mcp)")

    # ── 8. Install CLAUDE.md instructions ────────────────────────────────
    console.print("\n[bold]8. Installing Claude instructions[/bold]")
    instructions_installed = _install_claude_instructions(claude_dir, force)
    if instructions_installed:
        console.print(f"  [green]✓[/green] CAP instructions → ~/.claude/CLAUDE.md")
    else:
        console.print(f"  [dim]─[/dim] CAP instructions already present")

    # ── 9. Save manifest ──────────────────────────────────────────────────
    manifest["version"] = "0.3.0"
    manifest["cap_home"] = str(cap_home)
    manifest["python"] = sys.executable
    manifest["installed_at"] = datetime.now(timezone.utc).isoformat()
    _save_manifest(cap_home, manifest)

    # ── Summary ───────────────────────────────────────────────────────────
    console.print("\n" + "─" * 60)
    console.print(Panel(
        "[bold green]CAP initialized successfully![/bold green]\n\n"
        "[bold]Quick start:[/bold]\n"
        "  [cyan]cap status[/cyan]          — Platform overview\n"
        "  [cyan]cap doctor[/cyan]          — Health check\n"
        "  [cyan]cap workflow demo[/cyan]   — Team simulation demo\n"
        "  [cyan]cap workflow daemon[/cyan] — Auto-render workflows\n"
        "  [cyan]cap eval run[/cyan]        — Run quality evaluations\n"
        "\n[bold]In Claude Code:[/bold]\n"
        "  Workflows render as team conversations automatically.\n"
        "  MCP tools: knowledge_ingest, session_start, fleet_status, workflow_start\n"
        "\n[bold]Safety:[/bold]\n"
        f"  Config backups stored in {_backups_dir()}\n"
        "  Run [cyan]cap uninstall[/cyan] to cleanly restore original configs.",
        box=box.ROUNDED,
    ))


# ── cap uninstall ─────────────────────────────────────────────────────────────

@click.command()
@click.option("--keep-data", is_flag=True, help="Keep databases (remove config + servers only)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def uninstall(keep_data: bool, yes: bool):
    """Remove all CAP platform artifacts and restore original configs."""
    cap_home = _cap_home()
    claude_dir = _claude_dir()

    if not cap_home.exists():
        console.print("[yellow]CAP is not installed (no ~/.claude-platform found).[/yellow]")
        return

    manifest = _load_manifest(cap_home)

    # ── Show what will happen ─────────────────────────────────────────────
    table = Table(title="Uninstall Plan", box=box.ROUNDED, show_edge=True)
    table.add_column("Action", style="bold")
    table.add_column("Details")
    table.add_column("Reversible?", justify="center")

    table.add_row(
        "Deregister MCP servers",
        f"{len(manifest.get('mcp_servers', []))} servers",
        "[green]yes[/green]",
    )
    table.add_row(
        "Remove agents",
        f"{len(manifest.get('installed_agents', []))} agent definitions",
        "[green]yes[/green] (reinstall)",
    )
    table.add_row(
        "Remove workflows",
        f"{len(manifest.get('installed_workflows', []))} workflow scripts",
        "[green]yes[/green] (reinstall)",
    )
    table.add_row(
        "Restore configs",
        "~/.claude.json, ~/.claude/settings.json",
        "[green]yes[/green] (from backup)",
    )
    if keep_data:
        table.add_row(
            "Keep databases",
            f"4 databases in {cap_home / 'data'}",
            "[dim]preserved[/dim]",
        )
    else:
        table.add_row(
            "[red]Remove platform data[/red]",
            f"All files in {cap_home}",
            "[red]NO[/red] (data lost)",
        )

    console.print(table)
    console.print()

    if not yes:
        click.confirm("Proceed with uninstall?", abort=True)

    # ── 1. Deregister MCP servers ─────────────────────────────────────────
    console.print("\n[bold]1. Deregistering MCP servers[/bold]")
    for name in manifest.get("mcp_servers", []):
        ok = _run_claude_mcp(["remove", "--scope", "user", name])
        if ok:
            console.print(f"  [green]✓[/green] Removed {name}")
        else:
            console.print(f"  [yellow]![/yellow] {name} — may need manual removal")

    # ── 2. Remove agents ──────────────────────────────────────────────────
    console.print("\n[bold]2. Removing agents[/bold]")
    agents_dir = claude_dir / "agents"
    removed_agents = 0
    for agent_name in manifest.get("installed_agents", []):
        agent_path = agents_dir / f"{agent_name}.md"
        if agent_path.exists():
            agent_path.unlink()
            removed_agents += 1
    console.print(f"  [green]✓[/green] {removed_agents} agents removed")

    # ── 3. Remove workflows ───────────────────────────────────────────────
    console.print("\n[bold]3. Removing workflows[/bold]")
    workflows_dir = claude_dir / "workflows"
    removed_workflows = 0
    for wf_name in manifest.get("installed_workflows", []):
        wf_path = workflows_dir / f"{wf_name}.js"
        if wf_path.exists():
            wf_path.unlink()
            removed_workflows += 1
    console.print(f"  [green]✓[/green] {removed_workflows} workflows removed")

    # ── 4. Restore user configs from backup ───────────────────────────────
    console.print("\n[bold]4. Restoring user configs[/bold]")
    restore_results = _restore_user_configs()

    for label, success in restore_results.items():
        if success:
            console.print(f"  [green]✓[/green] Restored {label}")
        else:
            console.print(f"  [dim]─[/dim] {label} — no backup found (was not modified by CAP)")

    # ── 5. Remove Claude instructions ────────────────────────────────────
    console.print("\n[bold]5. Removing Claude instructions[/bold]")
    if _uninstall_claude_instructions(claude_dir):
        console.print(f"  [green]✓[/green] CAP instructions removed from CLAUDE.md")
    else:
        console.print(f"  [dim]─[/dim] No CAP instructions found in CLAUDE.md")

    # ── 6. Remove platform data ───────────────────────────────────────────
    if keep_data:
        console.print("\n[bold]6. Keeping databases[/bold] (--keep-data)")
        for item in cap_home.iterdir():
            if item.name in ("data", "backups"):
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        console.print(f"  [green]✓[/green] Config removed, data preserved at {cap_home / 'data'}")
    else:
        console.print("\n[bold]6. Removing platform directory[/bold]")
        shutil.rmtree(cap_home)
        console.print(f"  [green]✓[/green] {cap_home} removed")

    # ── Done ──────────────────────────────────────────────────────────────
    console.print("\n" + "─" * 60)
    console.print(Panel(
        "[bold green]Uninstall complete.[/bold green]\n\n"
        "Your original configs have been restored.\n"
        "Your system is back to its pre-CAP state.\n\n"
        "[dim]To also remove the Python package:[/dim]\n"
        "  [cyan]uv tool uninstall claude-agent-platform[/cyan]",
        box=box.ROUNDED,
    ))


# ── cap backup (utility) ─────────────────────────────────────────────────────

@click.command()
def backup():
    """Create a manual backup of current configs."""
    backups = _backup_user_configs()

    if backups:
        console.print("[bold]Backups created:[/bold]")
        for label, path in backups.items():
            console.print(f"  [green]✓[/green] {label} → {path}")
    else:
        console.print("[dim]No configs found to backup.[/dim]")

    # Also backup CAP's own config
    cap_config = _cap_home() / "config.toml"
    if cap_config.exists():
        bp = _backup_file(cap_config, "cap-config-toml")
        if bp:
            console.print(f"  [green]✓[/green] cap-config-toml → {bp.name}")


@click.command("restore")
@click.option("--list", "list_backups", is_flag=True, help="List available backups without restoring")
def restore(list_backups: bool):
    """Restore configs from backup."""
    backups = _backups_dir()

    if not backups.exists():
        console.print("[yellow]No backups directory found.[/yellow]")
        return

    all_backups = sorted(backups.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

    if not all_backups:
        console.print("[dim]No backups found.[/dim]")
        return

    if list_backups:
        table = Table(title="Available Backups", box=box.SIMPLE_HEAD, show_edge=False)
        table.add_column("File", style="cyan")
        table.add_column("Size", justify="right")
        table.add_column("Created")

        for bp in all_backups:
            stat = bp.stat()
            size = f"{stat.st_size / 1024:.1f} KB"
            created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            table.add_row(bp.name, size, created)

        console.print(table)
        return

    # Restore latest of each type
    results = _restore_user_configs()
    for label, success in results.items():
        if success:
            console.print(f"[green]✓[/green] Restored {label}")
        else:
            console.print(f"[dim]─[/dim] {label} — no backup available")

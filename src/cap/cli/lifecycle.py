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


def _get_bundled_data_path() -> Path:
    """Get path to bundled data within the package."""
    return Path(str(pkg_files("cap.data")))


# ── MCP registration helpers ─────────────────────────────────────────────────

def _run_claude_mcp(args: list[str]) -> bool:
    """Run `claude mcp ...` command. Returns True on success or already-exists."""
    try:
        result = subprocess.run(
            ["claude", "mcp"] + args,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True
        combined = (result.stderr + result.stdout).lower()
        if "already exists" in combined:
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _mcp_server_exists(name: str) -> bool:
    """Check if an MCP server is already registered."""
    claude_json = _claude_json_path()
    if not claude_json.exists():
        return False
    try:
        data = json.loads(claude_json.read_text())
        return name in data.get("mcpServers", {})
    except (json.JSONDecodeError, KeyError):
        return False


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
    """Backup all user config files that CAP might modify. Returns {label: backup_path}.

    Only backs up on FIRST init — if a backup already exists for a label, skip it
    to avoid polluting the backup chain with CAP-modified configs.
    """
    backups_made = {}

    configs_to_backup = [
        (_claude_json_path(), "claude-json"),
        (_settings_json_path(), "settings-json"),
        (_claude_dir() / "CLAUDE.md", "claude-md"),
    ]

    for file_path, label in configs_to_backup:
        # Skip if a pristine backup already exists
        if _get_latest_backup(label) is not None:
            continue
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
        ("claude-md", _claude_dir() / "CLAUDE.md"),
    ]

    for label, target in restores:
        results[label] = _restore_file(label, target)

    return results


# ── CLAUDE.md ────────────────────────────────────────────────────────────────

_CLAUDE_MD_CONTENT = """\
# Claude Code — Global Instructions

## Security

- Use SSH-only repository URLs for clone and fetch operations.
- Never store or print secrets, tokens, private keys, or credentials.
- Never commit `.env` files, credentials, or AWS keys.

## AWS Access

- Run `aws sso login --sso-session moia` to authenticate.
- Ask which AWS profile/role to use before first AWS CLI call in a session.
- Default to read-only profile if none specified.
- Pass `--profile <name>` explicitly on every call.

## Communication Style

- Terse. Results first.
- Never ask technical architecture questions — make those calls yourself.
- Only ask: business decisions, access/credential blockers, or approval gates (push, PR, deploy, apply).

## Autonomy

**Just do it (no approval needed):**
File reads/edits, local commits, branch creation, builds, tests, linting, terraform plan/validate, research.

**Approval gates (stop and ask):**
Push to remote, create PR, merge, terraform apply, cdk deploy, kubectl apply, destructive commands.

## Information Retrieval (STRICT ORDER — MANDATORY)

**BEFORE using bash find/grep/cat to answer ANY question about repos, architecture, or configs, you MUST first call the knowledge_search MCP tool.**

Step 1 — Load the tool schema (required because MCP tools are deferred):
```
ToolSearch({ query: "select:mcp__cap-knowledge__knowledge_search" })
```

Step 2 — Call the tool:
```
mcp__cap-knowledge__knowledge_search({ query: "your search terms", workspace: "<workspace-path>" })
```

Full retrieval priority:
1. `mcp__cap-knowledge__knowledge_search` — FTS5 + semantic + graph across all indexed repos
2. `mcp__cap-knowledge__knowledge_graph_query` — traverse service/resource relationships
3. `mcp__cap-session__session_recall` — past decisions, learnings, corrections
4. Bash grep/find — ONLY for exact file contents not yet indexed, or for execution

**NEVER skip straight to bash.** The knowledge base is faster and more complete than filesystem traversal.

## Session Memory

- Use `session_start` at the beginning of complex work
- Use `session_record` to persist decisions and learnings during work
- Use `session_feedback` when the user corrects you — it persists across sessions

## What NOT to do

- Do NOT use bash grep/find to search for information when `knowledge_search` can answer it
- Do NOT re-discover repo structure manually — the knowledge graph has it indexed
- Do NOT read `~/.claude/knowledge/` files — they are legacy static files
- Do NOT ignore session corrections — they persist specifically so you won't repeat mistakes
"""


def _install_claude_md(force: bool = False) -> bool:
    """Create ~/.claude/CLAUDE.md with CAP instructions. Returns True if written."""
    claude_dir = _claude_dir()
    claude_md = claude_dir / "CLAUDE.md"

    if claude_md.exists() and not force:
        return False

    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(_CLAUDE_MD_CONTENT)
    return True


# ── Settings.json ────────────────────────────────────────────────────────────

_CAP_MCP_PERMISSIONS = [
    # Core Claude Code orchestration — required for auto-orchestration
    "Workflow(*)",
    "Agent(*)",
    # Knowledge server tools
    "mcp__cap-knowledge__knowledge_search",
    "mcp__cap-knowledge__knowledge_graph_query",
    "mcp__cap-knowledge__knowledge_graph_add",
    "mcp__cap-knowledge__knowledge_ingest",
    "mcp__cap-knowledge__knowledge_record",
    "mcp__cap-knowledge__knowledge_status",
    "mcp__cap-knowledge__knowledge_sync",
    # Session server tools
    "mcp__cap-session__session_start",
    "mcp__cap-session__session_record",
    "mcp__cap-session__session_recall",
    "mcp__cap-session__session_feedback",
    "mcp__cap-session__session_end",
    "mcp__cap-session__session_checkpoint",
    "mcp__cap-session__session_history",
    # Fleet server tools
    "mcp__cap-fleet__fleet_status",
    "mcp__cap-fleet__fleet_health_check",
    "mcp__cap-fleet__fleet_discover",
    "mcp__cap-fleet__fleet_logs",
    # Workflow engine tools
    "mcp__workflow-engine__workflow_start",
    "mcp__workflow-engine__workflow_status",
    "mcp__workflow-engine__workflow_kill",
    "mcp__workflow-engine__workflow_list",
    "mcp__workflow-engine__workflow_estimate",
    "mcp__workflow-engine__workflow_report",
    "mcp__workflow-engine__workflow_signal",
]


def _install_settings_permissions() -> bool:
    """Add CAP MCP tool permissions to ~/.claude/settings.json. Returns True if modified."""
    settings_path = _settings_json_path()

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        _claude_dir().mkdir(parents=True, exist_ok=True)
        settings = {}

    permissions = settings.setdefault("permissions", {})
    allow_list = permissions.setdefault("allow", [])

    added = 0
    for perm in _CAP_MCP_PERMISSIONS:
        if perm not in allow_list:
            allow_list.append(perm)
            added += 1

    if added == 0:
        return False

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def _remove_settings_permissions() -> bool:
    """Remove CAP MCP tool permissions from settings.json. Returns True if modified."""
    settings_path = _settings_json_path()
    if not settings_path.exists():
        return False

    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return False

    allow_list = settings.get("permissions", {}).get("allow", [])
    original_len = len(allow_list)
    allow_list[:] = [p for p in allow_list if p not in _CAP_MCP_PERMISSIONS]

    if len(allow_list) == original_len:
        return False

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
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


# ── MCP Server Definitions ───────────────────────────────────────────────────

def _get_platform_mcp_servers() -> list[dict]:
    """Platform MCP servers (AWS, K8s, Terraform) — installed if not already present."""
    return [
        {
            "name": "kubernetes",
            "command": "npx",
            "args": ["-y", "mcp-server-kubernetes"],
            "env": [],
        },
        {
            "name": "aws-docs",
            "command": "uvx",
            "args": ["awslabs.aws-documentation-mcp-server@latest"],
            "env": [],
        },
        {
            "name": "aws-iam",
            "command": "uvx",
            "args": ["awslabs.iam-mcp-server"],
            "env": [],
        },
        {
            "name": "aws-eks",
            "command": "uvx",
            "args": ["awslabs.eks-mcp-server"],
            "env": [],
        },
        {
            "name": "aws-cloudwatch",
            "command": "uvx",
            "args": ["awslabs.cloudwatch-mcp-server"],
            "env": [],
        },
        {
            "name": "aws-lambda",
            "command": "uvx",
            "args": ["awslabs.lambda-tool-mcp-server"],
            "env": [],
        },
        {
            "name": "aws-pricing",
            "command": "uvx",
            "args": ["awslabs.aws-pricing-mcp-server"],
            "env": [],
        },
        {
            "name": "aws-iac",
            "command": "uvx",
            "args": ["awslabs.aws-iac-mcp-server"],
            "env": [],
        },
        {
            "name": "terraform",
            "command": "npx",
            "args": ["-y", "terraform-mcp-server"],
            "env": [],
        },
    ]


def _get_cap_mcp_servers(cap_home: Path, data_dir: Path) -> list[dict]:
    """CAP's own MCP servers — always installed fresh."""
    python_bin = sys.executable
    servers_dir = Path(__file__).parent.parent / "servers"

    return [
        {
            "name": "cap-knowledge",
            "command": python_bin,
            "args": [str(servers_dir / "knowledge_server.py")],
            "env": [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"],
        },
        {
            "name": "cap-session",
            "command": python_bin,
            "args": [str(servers_dir / "session_server.py")],
            "env": [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"],
        },
        {
            "name": "cap-fleet",
            "command": python_bin,
            "args": [str(servers_dir / "fleet_server.py")],
            "env": [f"CAP_HOME={cap_home}", f"PYTHONPATH={cap_home}"],
        },
        {
            "name": "workflow-engine",
            "command": python_bin,
            "args": [str(servers_dir / "workflow_server.py")],
            "env": [f"PLATFORM_DATA_DIR={data_dir}", f"PYTHONPATH={cap_home}"],
        },
    ]


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
        if db_path.exists():
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

        # Platform servers (AWS, K8s, Terraform) — only if not already present
        platform_servers = _get_platform_mcp_servers()
        for srv in platform_servers:
            if _mcp_server_exists(srv["name"]):
                console.print(f"  [dim]─[/dim] {srv['name']} (already registered)")
                continue
            cmd_args = ["add", srv["name"], "--scope", "user"]
            for var in srv["env"]:
                cmd_args.extend(["-e", var])
            cmd_args.extend(["--", srv["command"]] + srv["args"])
            ok = _run_claude_mcp(cmd_args)
            if ok:
                console.print(f"  [green]✓[/green] {srv['name']}")
            else:
                console.print(f"  [yellow]![/yellow] {srv['name']} — failed")

        # CAP servers — always register (remove first if force)
        cap_servers = _get_cap_mcp_servers(cap_home, data_dir)
        registered = []
        for srv in cap_servers:
            if force:
                _run_claude_mcp(["remove", "--scope", "user", srv["name"]])
            cmd_args = ["add", srv["name"], "--scope", "user"]
            for var in srv["env"]:
                cmd_args.extend(["-e", var])
            cmd_args.extend(["--", str(srv["command"])] + srv["args"])
            ok = _run_claude_mcp(cmd_args)
            if ok:
                console.print(f"  [green]✓[/green] {srv['name']}")
                registered.append(srv["name"])
            else:
                console.print(f"  [yellow]![/yellow] {srv['name']} — failed (register manually with `claude mcp add`)")
        manifest["mcp_servers"] = registered
    else:
        console.print("\n[bold]7. MCP servers[/bold] — skipped (--skip-mcp)")

    # ── 8. CLAUDE.md ─────────────────────────────────────────────────────
    console.print("\n[bold]8. Installing Claude instructions[/bold]")
    if _install_claude_md(force):
        console.print(f"  [green]✓[/green] ~/.claude/CLAUDE.md created")
    else:
        console.print(f"  [dim]─[/dim] ~/.claude/CLAUDE.md exists (use --force to overwrite)")

    # ── 9. Settings permissions ───────────────────────────────────────────
    console.print("\n[bold]9. Configuring MCP tool permissions[/bold]")
    if _install_settings_permissions():
        console.print(f"  [green]✓[/green] {len(_CAP_MCP_PERMISSIONS)} tool permissions added to settings.json")
    else:
        console.print(f"  [dim]─[/dim] Permissions already configured")

    # ── 10. Save manifest ─────────────────────────────────────────────────
    manifest["version"] = "0.3.0"
    manifest["cap_home"] = str(cap_home)
    manifest["python"] = sys.executable
    manifest["installed_at"] = datetime.now(timezone.utc).isoformat()
    _save_manifest(cap_home, manifest)

    # ── Summary ───────────────────────────────────────────────────────────
    console.print("\n" + "─" * 60)
    console.print(Panel(
        "[bold green]CAP initialized successfully![/bold green]\n\n"
        "[bold]Next steps:[/bold]\n"
        "  [cyan]cap sync --workspace ~/path/to/code[/cyan]  — Index your codebase\n"
        "  [cyan]cap status[/cyan]                           — Platform overview\n"
        "  [cyan]cap doctor[/cyan]                           — Health check\n"
        "\n[bold]In Claude Code:[/bold]\n"
        "  Reload window (Cmd+Shift+P → Reload) to pick up new MCP servers.\n"
        "  Then ask anything — Claude will use knowledge_search automatically.\n"
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
        "Deregister CAP MCP servers",
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
        "Remove CAP-owned configs",
        "CAP permissions from settings.json; CLAUDE.md if CAP-generated",
        "[green]yes[/green] (surgical removal)",
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

    # ── 1. Deregister CAP MCP servers ────────────────────────────────────
    console.print("\n[bold]1. Deregistering CAP MCP servers[/bold]")
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

    # ── 4. Remove CAP MCP permissions from settings ──────────────────────
    console.print("\n[bold]4. Removing MCP tool permissions[/bold]")
    if _remove_settings_permissions():
        console.print(f"  [green]✓[/green] CAP permissions removed from settings.json")
    else:
        console.print(f"  [dim]─[/dim] No CAP permissions found")

    # ── 5. Surgical config cleanup ────────────────────────────────────────
    # Do NOT wholesale-restore from backup: restoring ~/.claude.json would undo
    # the claude mcp remove calls we just made above, and restoring settings.json
    # would undo the permission removal already done in step 4.
    # Instead, only remove artefacts that CAP owns outright.
    console.print("\n[bold]5. Removing CAP-owned config artefacts[/bold]")

    # CLAUDE.md — remove only if the file is entirely CAP-generated
    claude_md = _claude_dir() / "CLAUDE.md"
    if claude_md.exists() and claude_md.read_text().startswith(_CLAUDE_MD_CONTENT[:120]):
        claude_md.unlink()
        console.print(f"  [green]✓[/green] ~/.claude/CLAUDE.md removed (was CAP-generated)")
    else:
        console.print(f"  [dim]─[/dim] ~/.claude/CLAUDE.md left untouched (user-customised or absent)")

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
        "CAP permissions and CAP-generated CLAUDE.md have been removed.\n"
        "Platform MCP servers (AWS, K8s, Terraform) were left untouched.\n\n"
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

    results = _restore_user_configs()
    for label, success in results.items():
        if success:
            console.print(f"[green]✓[/green] Restored {label}")
        else:
            console.print(f"[dim]─[/dim] {label} — no backup available")

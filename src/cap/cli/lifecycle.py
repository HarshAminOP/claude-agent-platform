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

Cold Start Flow (from CAP System Design v1 Section 10):
    Phase 0 (0s):   Create directories
    Phase 1 (0-2s): Create cap.db, migrate, write default config.toml
    Phase 2 (2-3s): Generate hook scripts, update settings.json with hook entries
    Phase 3 (3-5s): Quick-index current workspace (README, package.json, etc.)
    Phase 4 (5-10s): Register MCP servers in settings.json
    Phase 5 (bg):   Queue full AST index for next session_start
    Phase 6:        Health check (DB, hooks syntax, MCP configs)
    Phase 7:        Print success summary with next steps
"""

import hashlib
import importlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib.resources import files as pkg_files
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(stderr=True)
logger = logging.getLogger("cap.cli.lifecycle")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _cap_home() -> Path:
    from cap.config import get_cap_home
    return get_cap_home()


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

def _get_claude_md_content() -> str:
    """Load CLAUDE.md template from the bundled data package."""
    template_path = _get_bundled_data_path() / "claude_md_template.md"
    if template_path.exists():
        return template_path.read_text()
    # Fallback: use the currently installed CLAUDE.md if available
    existing = _claude_dir() / "CLAUDE.md"
    if existing.exists():
        return existing.read_text()
    return _CLAUDE_MD_FALLBACK


_CLAUDE_MD_FALLBACK = """\
# Claude Code — Global Instructions

<!-- STOP. READ THIS FIRST. DO NOT SKIP. -->

## HARD RULE: DELEGATE, DO NOT IMPLEMENT

**If YOUR response (the outer session) would contain more than 20 lines of code, STOP. You are doing it wrong.**

This rule applies to the outer Claude session only. Subagents spawned by the orchestrator ARE allowed to write unlimited code — that is their job.

You are a delegation layer. Your ONLY job is to call:
```
Agent({ subagent_type: "orchestrator", description: "...", prompt: "..." })
```
The orchestrator has a full engineering team. YOU do not write code.

---

## MANDATORY BEHAVIOR — READ BEFORE ANYTHING ELSE

1. **ALL non-trivial tasks go to the orchestrator agent.** Delegate immediately:
   `Agent({ subagent_type: "orchestrator", prompt: "<task description>" })`
   Only exception: pure search → `Agent({ subagent_type: "Explore", prompt: "..." })`

2. **You MUST call session_record after every significant action.** Load via ToolSearch first:
   ```
   ToolSearch({ query: "select:mcp__cap-session__session_record" })
   ```
   Then call:
   ```
   mcp__cap-session__session_record({ event_type: "decision", content: "...", workspace: "<cwd>" })
   ```

3. **You MUST call knowledge_search BEFORE using bash grep/find.** No exceptions.

4. **You are a Product Owner interface, NOT an engineer.** Delegate to specialist agents, review output, report results. You do NOT write code directly unless it's a 1-line fix.

---

## Auto-Orchestration Rules

**These rules are AUTOMATIC. Apply the correct orchestration pattern on EVERY task.**

### Default Routing

**Every non-trivial task → orchestrator.** No exceptions, no routing decisions.

```
Agent({
  subagent_type: "orchestrator",
  description: "Short task summary",
  prompt: "Full task description. Include file paths, context, acceptance criteria."
})
```

The orchestrator will internally spawn: dev, devops, security, sre, code-review, test, optimization, docs, cicd, or aws-architect agents as needed.

**You do NOT need to figure out which specialist to use.** That's the orchestrator's job.

Only bypass the orchestrator for:
- Trivial 1-line fixes (do inline)
- Quick lookups / status checks (do inline)
- Pure code search (`Agent({ subagent_type: "Explore", prompt: "..." })`)

NEVER write more than 20 lines of code yourself. Delegate.

---

## Security

- Use SSH-only repository URLs for clone and fetch operations.
- Never store or print secrets, tokens, private keys, or credentials.
- Never commit `.env` files, credentials, or AWS keys.

## AWS Access

- Run `aws sso login --sso-session <your-sso-session>` to authenticate.
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
mcp__cap-knowledge__knowledge_search({ query: "your search terms" })
```

Full retrieval priority:
1. `mcp__cap-knowledge__knowledge_search` — FTS5 + semantic + graph across all indexed repos
2. `mcp__cap-knowledge__knowledge_graph_query` — traverse service/resource relationships
3. `mcp__cap-session__session_recall` — past decisions, learnings, corrections
4. Bash grep/find — ONLY for exact file contents not yet indexed, or for execution

**NEVER skip straight to bash.** The knowledge base is faster and more complete than filesystem traversal.

## Session Memory (MANDATORY — every session)

Step 1: Load session tools at conversation start:
```
ToolSearch({ query: "select:mcp__cap-session__session_start,mcp__cap-session__session_record,mcp__cap-session__session_feedback" })
```

Step 2: Start or resume session:
```
mcp__cap-session__session_start({ workspace: "<current working directory>" })
```

Step 3: Record events throughout the conversation:
- After any decision: `session_record({ event_type: "decision", content: "...", workspace: "..." })`
- After user correction: `session_feedback({ what_was_wrong: "...", what_is_correct: "...", workspace: "..." })`
- After discovery: `session_record({ event_type: "discovery", content: "...", workspace: "..." })`

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
    claude_md.write_text(_get_claude_md_content())
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
    # Diagram server tools
    "mcp__cap-diagram__diagram_render",
    "mcp__cap-diagram__diagram_engines",
    "mcp__cap-diagram__diagram_mermaid_to_md",
    # Knowledge resolver tools
    "mcp__cap-knowledge__knowledge_resolve_repo",
    "mcp__cap-knowledge__knowledge_resolve_deps",
    # Backlog, decisions, conflicts tools
    "mcp__cap-backlog__backlog_create",
    "mcp__cap-backlog__backlog_claim",
    "mcp__cap-backlog__backlog_complete",
    "mcp__cap-backlog__backlog_verify",
    "mcp__cap-backlog__backlog_list",
    "mcp__cap-backlog__backlog_stats",
    "mcp__cap-backlog__backlog_update",
    "mcp__cap-backlog__decision_propose",
    "mcp__cap-backlog__decision_resolve",
    "mcp__cap-backlog__decision_list",
    "mcp__cap-backlog__conflict_raise",
    "mcp__cap-backlog__conflict_resolve",
    "mcp__cap-backlog__conflict_override",
    "mcp__cap-backlog__conflict_list",
    "mcp__cap-backlog__conflict_blocking",
    "mcp__cap-backlog__trace_record",
    "mcp__cap-backlog__trace_explain",
    "mcp__cap-backlog__blast_radius",
    "mcp__cap-backlog__autonomy_check",
    "mcp__cap-backlog__autonomy_record",
    "mcp__cap-backlog__autonomy_levels",
    # AST server tools
    "mcp__cap-ast__ast_search",
    "mcp__cap-ast__ast_match",
    "mcp__cap-ast__ast_refactor",
    # Code Intelligence server tools
    "mcp__cap-code-intel__code_structure",
    "mcp__cap-code-intel__code_dependents",
    "mcp__cap-code-intel__code_trace",
    "mcp__cap-code-intel__blast_radius",
    "mcp__cap-code-intel__code_search",
    "mcp__cap-code-intel__reindex",
    # Orchestrator server tools
    "mcp__cap-orchestrator__cap_route",
    "mcp__cap-orchestrator__cap_plan",
    "mcp__cap-orchestrator__cap_execute",
    "mcp__cap-orchestrator__cap_resume",
    "mcp__cap-orchestrator__cap_status",
    "mcp__cap-orchestrator__cap_dlq_list",
    "mcp__cap-orchestrator__cap_health",
    # Harness server tools
    "mcp__cap-harness__agent_spawn",
    "mcp__cap-harness__agent_execute",
    "mcp__cap-harness__agent_status",
    "mcp__cap-harness__agent_terminate",
    "mcp__cap-harness__agent_cost",
    "mcp__cap-harness__agent_health",
    "mcp__cap-harness__agent_pool",
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


def _build_hooks_config() -> dict:
    """Build the hooks configuration dict dynamically using the current Python path.

    Hook scripts are generated at ~/.claude/pretool.py and ~/.claude/posttool.py.
    This function builds hook entries that invoke those scripts with the correct
    Python interpreter path, ensuring they work regardless of how Claude Code
    was installed (uv, pipx, system python, etc.).
    """
    # Use python3 (not sys.executable) for portability — hooks run in user shell
    # where python3 resolves via PATH. sys.executable may point to a venv-specific
    # binary that doesn't exist after uv tool install.
    python_cmd = "python3"
    pretool_script = "~/.claude/pretool.py"
    posttool_script = "~/.claude/posttool.py"

    # PreToolUse: enforcement reminder + record tool start
    pre_tool_matchers = ["Edit", "Write", "NotebookEdit"]
    pre_tool_entries = []
    for matcher in pre_tool_matchers:
        pre_tool_entries.append({
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        f'{python_cmd} {pretool_script} '
                        '<<< \'{"tool_name": "' + matcher + '"}\' 2>/dev/null; '
                        'echo \'REMINDER: If writing >20 lines of code, '
                        'STOP and delegate to Agent({ subagent_type: "orchestrator" }). '
                        "Only 1-line fixes are allowed inline.'"
                    ),
                }
            ],
        })

    # PostToolUse: record tool completion for health tracking
    post_tool_matchers = ["Agent", "Bash", "Edit", "Write"]
    post_tool_entries = []
    for matcher in post_tool_matchers:
        post_tool_entries.append({
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        f'{python_cmd} {posttool_script} '
                        '<<< \'{"tool_name": "' + matcher + '", "success": true}\' 2>/dev/null'
                    ),
                }
            ],
        })

    return {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "mkdir -p ${CAP_HOME:-~/.claude-platform}/data && "
                            'echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) session_active workspace=$(pwd)" '
                            ">> ${CAP_HOME:-~/.claude-platform}/data/session_activity.log"
                        ),
                    }
                ],
            }
        ],
        "PreToolUse": pre_tool_entries,
        "PostToolUse": post_tool_entries,
    }


# Legacy static config (kept for backward compat with tests that import it)
_CAP_HOOKS = {
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        "mkdir -p ${CAP_HOME:-~/.claude-platform}/data && "
                        'echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) session_active workspace=$(pwd)" '
                        ">> ${CAP_HOME:-~/.claude-platform}/data/session_activity.log"
                    ),
                }
            ],
        }
    ],
    "PreToolUse": [
        {
            "matcher": "Edit",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        'echo \'REMINDER: If you are writing more than 20 lines of code, '
                        "STOP and delegate to Agent({ subagent_type: \"orchestrator\" }) "
                        "instead. Only 1-line fixes are allowed inline.'"
                    ),
                }
            ],
        },
        {
            "matcher": "Write",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        'echo \'REMINDER: If you are creating implementation files, '
                        "STOP and delegate to Agent({ subagent_type: \"orchestrator\" }) "
                        "instead. Only trivial files are allowed inline.'"
                    ),
                }
            ],
        },
    ],
    "PostToolUse": [
        {
            "matcher": "Agent",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        "mkdir -p ${CAP_HOME:-~/.claude-platform}/data && "
                        'echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) agent_dispatched workspace=$(pwd)" '
                        ">> ${CAP_HOME:-~/.claude-platform}/data/agent_activity.log"
                    ),
                }
            ],
        }
    ],
}


def _install_hooks() -> bool:
    """Add CAP hooks to settings.json. Returns True if modified.

    Uses _build_hooks_config() for the dynamic config (correct Python path
    and script locations). Falls back gracefully if scripts don't exist yet.
    """
    settings_path = _settings_json_path()

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        _claude_dir().mkdir(parents=True, exist_ok=True)
        settings = {}

    # Use dynamic hook config that references the correct Python interpreter
    cap_hooks = _build_hooks_config()

    hooks = settings.setdefault("hooks", {})
    modified = False

    for event, hook_list in cap_hooks.items():
        if event not in hooks:
            hooks[event] = hook_list
            modified = True
        else:
            # Check if our hooks are already present (by matcher)
            existing_matchers = {
                entry.get("matcher", "") for entry in hooks[event]
            }
            for hook_entry in hook_list:
                matcher = hook_entry.get("matcher", "")
                if matcher not in existing_matchers:
                    hooks[event].append(hook_entry)
                    modified = True

    if not modified:
        return False

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def _generate_hook_scripts(claude_dir: Path, data_dir: Path) -> bool:
    """Generate thin-wrapper hook scripts in ~/.claude/ directory.

    These scripts delegate to the installed cap.hooks package, ensuring that
    when CAP is updated via `uv tool install --force`, the hook logic is always
    current without needing to re-run `cap init`.

    Returns True if scripts were generated or updated.
    """
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Hook name -> module path in the installed package
    hook_wrappers = {
        "pretool.py": "cap.hooks.pretool",
        "posttool.py": "cap.hooks.posttool",
    }

    generated = False
    for filename, module_path in hook_wrappers.items():
        hook_path = claude_dir / filename
        wrapper_content = f'''#!/usr/bin/env python3
"""CAP {filename.replace('.py', '').title()} hook — thin wrapper that delegates to installed package.

Generated by `cap init`. Do NOT put logic here — it lives in {module_path}.
When CAP is updated via `uv tool install --force`, the new code runs automatically.
"""
import sys

try:
    from {module_path} import main
    sys.exit(main() or 0)
except ImportError:
    pass  # CAP not installed, no-op
'''
        # Always overwrite to ensure wrapper is current (not stale inline logic)
        if hook_path.exists():
            existing = hook_path.read_text()
            # Only skip if already a correct thin wrapper
            if f"from {module_path} import main" in existing:
                continue

        hook_path.write_text(wrapper_content)
        os.chmod(hook_path, 0o755)
        generated = True

    return generated


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
        "version": "0.5.0",
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
    """CAP's own MCP servers — registered via pip console_scripts entry points.

    No PYTHONPATH or absolute paths: entry-point commands are resolved from
    the PATH after ``pip install -e .`` or ``uv tool install``.

    Environment variables for ALL servers are generated by cap.config.generate_mcp_env_list()
    which includes: CAP_HOME, AWS_PROFILE (if configured), AWS_DEFAULT_REGION (if configured).
    """
    from cap.config import generate_mcp_env_list

    # Base env for all servers: CAP_HOME + AWS_PROFILE + AWS_DEFAULT_REGION
    base_env = generate_mcp_env_list()

    # Orchestrator additionally needs CAP_ORCHESTRATOR_DB
    orchestrator_env = base_env + [f"CAP_ORCHESTRATOR_DB={data_dir / 'platform.db'}"]

    return [
        {
            "name": "cap-knowledge",
            "command": "cap-knowledge-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-session",
            "command": "cap-session-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-fleet",
            "command": "cap-fleet-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-workflow-engine",
            "command": "cap-workflow-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-diagram",
            "command": "cap-diagram-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-backlog",
            "command": "cap-backlog-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-ast",
            "command": "cap-ast-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-code-intel",
            "command": "cap-code-intel-server",
            "args": [],
            "env": base_env,
        },
        {
            "name": "cap-orchestrator",
            "command": "cap-orchestrator-server",
            "args": [],
            "env": orchestrator_env,
        },
        {
            "name": "cap-harness",
            "command": "cap-harness-server",
            "args": [],
            "env": base_env,
        },
    ]


# ── Workspace detection ──────────────────────────────────────────────────────

def _detect_workspace_repos(cwd: Path, max_depth: int = 2) -> list[Path]:
    """Auto-detect .git repos under CWD (max depth 2). Returns list of repo roots."""
    repos: list[Path] = []

    # Check if CWD itself is a git repo
    if (cwd / ".git").exists():
        repos.append(cwd)

    # Scan children up to max_depth
    for depth in range(1, max_depth + 1):
        pattern = "/".join(["*"] * depth) + "/.git"
        for git_dir in cwd.glob(pattern):
            if git_dir.is_dir():
                repos.append(git_dir.parent)

    return sorted(set(repos))


# ── Quick index helpers ──────────────────────────────────────────────────────

_QUICK_INDEX_FILES = [
    "README.md",
    "README.rst",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "tsconfig.json",
    "terraform.tf",
    "main.tf",
]


def _quick_index_workspace(data_dir: Path, workspace: Path, repos: list[Path]) -> int:
    """Quick-index key files from workspace repos into FTS5.

    Returns count of entries indexed.
    """
    knowledge_db = data_dir / "knowledge.db"
    if not knowledge_db.exists():
        return 0

    conn = sqlite3.connect(str(knowledge_db), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    indexed = 0
    for repo in repos:
        for filename in _QUICK_INDEX_FILES:
            filepath = repo / filename
            if not filepath.exists():
                continue
            try:
                content = filepath.read_text(errors="replace")
                if len(content) > 100_000:
                    content = content[:100_000]  # Truncate very large files

                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                entry_uuid = str(uuid.uuid4())
                workspace_str = str(workspace)
                source_path = str(filepath)
                title = f"{repo.name}/{filename}"

                # Check if already indexed (by source_path)
                existing = conn.execute(
                    "SELECT id FROM knowledge_entries WHERE source_path = ? AND workspace = ?",
                    (source_path, workspace_str),
                ).fetchone()
                if existing:
                    continue

                # Determine content_type from filename
                if filename.endswith(".md") or filename.endswith(".rst"):
                    content_type = "documentation"
                elif filename in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml"):
                    content_type = "manifest"
                elif filename in ("Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml"):
                    content_type = "build_config"
                elif filename in ("tsconfig.json", "terraform.tf", "main.tf"):
                    content_type = "config"
                else:
                    content_type = "file"

                conn.execute(
                    """INSERT INTO knowledge_entries
                       (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_uuid,
                        workspace_str,
                        source_path,
                        "quick_index",
                        content_type,
                        title,
                        content,
                        content_hash,
                        json.dumps({"repo": repo.name, "filename": filename, "indexed_at": time.time()}),
                    ),
                )
                indexed += 1
            except (OSError, UnicodeDecodeError):
                continue

    conn.commit()
    conn.close()
    return indexed


# ── Routing seed patterns ─────────────────────────────────────────────────────

def _load_routing_seed_patterns(data_dir: Path, force: bool = False) -> int:  # noqa: ARG001
    """Load pre-computed routing seed patterns for cold-start bootstrap.

    Delegates to ``EmbeddingRouter.load_seed_patterns()``.  Any failure is
    caught and logged so a seed-load error never blocks ``cap init``.

    Args:
        data_dir: Platform data directory (unused directly; EmbeddingRouter
            resolves the DB path internally via ``PLATFORM_DB_PATH``).
        force:    Passed through to ``load_seed_patterns`` to force re-seed.

    Returns:
        Number of seed pattern rows inserted, or 0 on any error.
    """
    try:
        from cap.harness.embed_router import EmbeddingRouter
        router = EmbeddingRouter()
        return router.load_seed_patterns(force=force)
    except Exception as exc:
        logger.warning("_load_routing_seed_patterns: failed: %s", exc)
        return 0


# ── Baseline corrections ─────────────────────────────────────────────────────

_BASELINE_CORRECTIONS = [
    {
        "what_was_wrong": "Using bash grep/find to search for information before checking knowledge base",
        "what_is_correct": "Always call knowledge_search MCP tool BEFORE using bash grep/find",
        "category": "information_retrieval",
    },
    {
        "what_was_wrong": "Writing more than 20 lines of code directly instead of delegating",
        "what_is_correct": "Delegate to Agent({ subagent_type: 'orchestrator' }) for any non-trivial implementation",
        "category": "delegation",
    },
    {
        "what_was_wrong": "Not recording session events after significant actions",
        "what_is_correct": "Call session_record after every decision, discovery, or correction",
        "category": "session_memory",
    },
    {
        "what_was_wrong": "Pushing code without running a code review first",
        "what_is_correct": "Always run code-review on the diff before any git push or PR creation",
        "category": "pre_push_review",
    },
    {
        "what_was_wrong": "Running multiple agents writing to the same repo without isolation",
        "what_is_correct": "Use EnterWorktree to give each parallel-writing agent an isolated working copy",
        "category": "parallel_writes",
    },
]


def _load_baseline_corrections(data_dir: Path, workspace: str) -> int:
    """Load baseline corrections into sessions.db. Returns count loaded."""
    sessions_db = data_dir / "sessions.db"
    if not sessions_db.exists():
        return 0

    conn = sqlite3.connect(str(sessions_db), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    loaded = 0
    for correction in _BASELINE_CORRECTIONS:
        # Check if already exists
        existing = conn.execute(
            "SELECT id FROM corrections WHERE what_was_wrong = ? AND workspace = ?",
            (correction["what_was_wrong"], workspace),
        ).fetchone()
        if existing:
            continue

        conn.execute(
            """INSERT INTO corrections (workspace, what_was_wrong, what_is_correct, category)
               VALUES (?, ?, ?, ?)""",
            (workspace, correction["what_was_wrong"], correction["what_is_correct"], correction["category"]),
        )
        loaded += 1

    conn.commit()
    conn.close()
    return loaded


# ── Git fetch helper ─────────────────────────────────────────────────────────

def _git_fetch_repos(repos: list[Path], timeout: int = 15) -> dict[str, bool]:
    """Run `git fetch --all` on detected repos in parallel. Returns {repo_name: success}."""
    results: dict[str, bool] = {}

    def _fetch_one(repo: Path) -> tuple[str, bool]:
        try:
            result = subprocess.run(
                ["git", "fetch", "--all", "--quiet"],
                cwd=str(repo),
                capture_output=True, text=True, timeout=timeout,
            )
            return (repo.name, result.returncode == 0)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return (repo.name, False)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_one, repo): repo for repo in repos}
        for future in as_completed(futures, timeout=timeout + 5):
            try:
                name, success = future.result()
                results[name] = success
            except Exception:
                results[futures[future].name] = False

    return results


# ── Health check ─────────────────────────────────────────────────────────────

def _run_health_check(cap_home: Path, data_dir: Path, claude_dir: Path) -> list[tuple[str, bool, str]]:
    """Run post-init health checks. Returns list of (check_name, passed, detail)."""
    checks: list[tuple[str, bool, str]] = []

    # Check 1: Databases exist and are valid SQLite
    for db_name in ["platform.db", "knowledge.db", "sessions.db", "fleet.db"]:
        db_path = data_dir / db_name
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                conn.execute("PRAGMA integrity_check")
                conn.close()
                checks.append((f"DB:{db_name}", True, "OK"))
            except sqlite3.Error as e:
                checks.append((f"DB:{db_name}", False, str(e)))
        else:
            checks.append((f"DB:{db_name}", False, "missing"))

    # Check 2: Hook scripts exist and have valid Python syntax
    for hook_name in ["pretool.py", "posttool.py"]:
        hook_path = claude_dir / hook_name
        if hook_path.exists():
            try:
                compile(hook_path.read_text(), str(hook_path), "exec")
                checks.append((f"Hook:{hook_name}", True, "valid syntax"))
            except SyntaxError as e:
                checks.append((f"Hook:{hook_name}", False, f"syntax error: {e}"))
        else:
            # Also check hooks subdirectory
            alt_path = claude_dir / "hooks" / hook_name
            if alt_path.exists():
                try:
                    compile(alt_path.read_text(), str(alt_path), "exec")
                    checks.append((f"Hook:{hook_name}", True, "valid syntax"))
                except SyntaxError as e:
                    checks.append((f"Hook:{hook_name}", False, f"syntax error: {e}"))
            else:
                checks.append((f"Hook:{hook_name}", False, "missing"))

    # Check 3: MCP server configs in settings.json
    settings_path = _settings_json_path()
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            hooks_config = settings.get("hooks", {})
            perms = settings.get("permissions", {}).get("allow", [])
            cap_perms = [p for p in perms if "cap-" in p or "cap_" in p or "workflow" in p]
            checks.append(("Settings:permissions", len(cap_perms) > 0, f"{len(cap_perms)} CAP permissions"))
            checks.append(("Settings:hooks", len(hooks_config) > 0, f"{len(hooks_config)} hook events"))
        except json.JSONDecodeError:
            checks.append(("Settings:json", False, "invalid JSON"))
    else:
        checks.append(("Settings:json", False, "missing"))

    # Check 4: config.toml exists
    config_path = cap_home / "config.toml"
    checks.append(("Config:toml", config_path.exists(), "exists" if config_path.exists() else "missing"))

    return checks


# ── Workspace auto-detection ─────────────────────────────────────────────────

def _resolve_workspace(workspace_arg: str | None) -> Path:
    """Resolve workspace path from CLI arg or CWD.

    If no arg given, uses CWD. If CWD itself is inside a git repo, walks up
    to find the git root and uses that as the workspace root.
    """
    if workspace_arg:
        return Path(workspace_arg).resolve()

    cwd = Path.cwd()
    # Walk up to find the nearest .git directory
    candidate = cwd
    while True:
        if (candidate / ".git").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            # Reached filesystem root — just use CWD
            break
        candidate = parent
    return cwd


# ── Python version check ──────────────────────────────────────────────────────

_MIN_PYTHON = (3, 11)


def _check_python_version() -> tuple[bool, str]:
    """Return (ok, message). ok=False if Python is too old."""
    major, minor = sys.version_info[:2]
    if (major, minor) < _MIN_PYTHON:
        return (
            False,
            f"Python {major}.{minor} detected. CAP requires Python "
            f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+. "
            f"Upgrade with: brew install python@{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]} "
            f"or https://python.org/downloads",
        )
    return True, f"Python {major}.{minor} OK"


# ── Post-init self-check ──────────────────────────────────────────────────────

def _run_post_init_verification(data_dir: Path) -> list[tuple[str, str, str]]:
    """Run a quick self-check after init completes.

    Returns list of (check, status, detail) rows for display.
    Status is one of: "yes", "no", or a count string.
    """
    rows: list[tuple[str, str, str]] = []

    # 1. Can we import cap?
    try:
        importlib.import_module("cap")
        rows.append(("cap importable", "yes", ""))
    except ImportError as exc:
        rows.append(("cap importable", "no", str(exc)[:60]))

    # 2. MCP servers registered (count from ~/.claude.json)
    claude_json = _claude_json_path()
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            count = len(data.get("mcpServers", {}))
            rows.append(("MCP servers registered", str(count), ""))
        except (json.JSONDecodeError, OSError):
            rows.append(("MCP servers registered", "0", "parse error"))
    else:
        rows.append((
            "MCP servers registered",
            "0",
            f"~/.claude.json not found — expected at {claude_json}",
        ))

    # 3. knowledge.db created?
    knowledge_db = data_dir / "knowledge.db"
    if knowledge_db.exists():
        rows.append(("knowledge.db created", "yes", f"{knowledge_db.stat().st_size // 1024} KB"))
    else:
        rows.append(("knowledge.db created", "no", f"expected at {knowledge_db}"))

    return rows


# ── Claude settings file check ────────────────────────────────────────────────

def _warn_if_settings_missing() -> str | None:
    """Return a warning string if settings.json does not exist yet, else None."""
    settings = _settings_json_path()
    if not settings.exists():
        return (
            f"Claude settings file not found at expected path: {settings}\n"
            "  CAP will create it. If Claude Code is not installed, hooks and\n"
            "  permissions will be written when you first launch Claude Code."
        )
    return None


# ── PATH check ────────────────────────────────────────────────────────────────

def _cap_on_path() -> bool:
    """Return True if the `cap` binary is resolvable on PATH right now."""
    return shutil.which("cap") is not None


# ── Minimal MCP server filter ─────────────────────────────────────────────────

#: Servers kept when --minimal is passed (KB + session + hooks, no heavy stack)
_MINIMAL_CAP_SERVER_NAMES = {"cap-knowledge", "cap-session"}


def _filter_cap_servers_minimal(servers: list[dict]) -> list[dict]:
    """Return only the servers needed for a minimal (KB-only) setup."""
    return [s for s in servers if s["name"] in _MINIMAL_CAP_SERVER_NAMES]


# ── Setup Wizard helpers ──────────────────────────────────────────────────────

# Provider constants
PROVIDER_AWS_BEDROCK = "aws-bedrock"
PROVIDER_ANTHROPIC_API = "anthropic-api"
PROVIDER_AZURE_OPENAI = "azure-openai"
PROVIDER_LOCAL = "local"

# Auth method constants for aws-bedrock
AUTH_SSO_PROFILE = "sso-profile"
AUTH_ENV_VARS = "env-vars"
AUTH_STATIC_CREDENTIALS = "static-credentials"
AUTH_INSTANCE_ROLE = "instance-role"

# Default model IDs per provider (non-Bedrock only; Bedrock uses region-aware probe)
_PROVIDER_MODEL_DEFAULTS = {
    PROVIDER_ANTHROPIC_API: {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-5-20250929",
        "opus": "claude-opus-4-6-20250918",
    },
    PROVIDER_LOCAL: {
        "haiku": "llama3",
        "sonnet": "codestral",
        "opus": "llama3:70b",
    },
}


def _detect_aws_profiles() -> list[str]:
    """Parse ~/.aws/config for available profile names (SSO and non-SSO)."""
    aws_config = Path.home() / ".aws" / "config"
    profiles = []
    if aws_config.exists():
        import configparser
        config = configparser.RawConfigParser()
        config.read(str(aws_config))
        for section in config.sections():
            if section == "default":
                profiles.insert(0, "default")
            elif section.startswith("profile "):
                profiles.append(section[8:])  # Strip "profile " prefix
    return profiles


def _detect_aws_credential_profiles() -> list[str]:
    """Parse ~/.aws/credentials for available static credential profile names."""
    aws_creds = Path.home() / ".aws" / "credentials"
    profiles = []
    if aws_creds.exists():
        import configparser
        config = configparser.RawConfigParser()
        config.read(str(aws_creds))
        for section in config.sections():
            profiles.append(section)
    return profiles


_MODEL_TIERS = {
    "economy": {
        "dev": "haiku", "devops": "haiku", "security": "haiku",
        "code-review": "haiku", "sre": "haiku", "test": "haiku",
        "docs": "haiku", "optimization": "haiku", "aws-architect": "haiku",
        "explore": "haiku", "cicd": "haiku",
    },
    "haiku-only": {
        "dev": "haiku", "devops": "haiku", "security": "haiku",
        "code-review": "haiku", "sre": "haiku", "test": "haiku",
        "docs": "haiku", "optimization": "haiku", "aws-architect": "haiku",
        "explore": "haiku", "cicd": "haiku",
    },
    "balanced": {
        "dev": "sonnet", "devops": "sonnet", "security": "opus",
        "code-review": "opus", "sre": "sonnet", "test": "sonnet",
        "docs": "haiku", "optimization": "haiku", "aws-architect": "opus",
        "explore": "sonnet", "cicd": "sonnet",
    },
    "quality": {
        "dev": "opus", "devops": "sonnet", "security": "opus",
        "code-review": "opus", "sre": "opus", "test": "sonnet",
        "docs": "sonnet", "optimization": "opus", "aws-architect": "opus",
        "explore": "sonnet", "cicd": "sonnet",
    },
}


def _run_setup_wizard(force: bool = False, non_interactive: bool = False) -> dict:
    """Interactive setup wizard for first-time CAP configuration.

    Supports multiple LLM providers and AWS authentication methods.
    Returns the complete harness config dict.
    """
    from cap.config import get_harness_config_path
    config_path = get_harness_config_path()

    if config_path.exists() and not force:
        return json.loads(config_path.read_text())

    # Auto-detect non-interactive mode when stdin is not a TTY (e.g. CI, tests)
    if not non_interactive and not sys.stdin.isatty():
        non_interactive = True

    console.print("\n[bold cyan]Setup Wizard[/bold cyan] — Configuring CAP for your environment\n")

    # ── Step 1: LLM Provider Selection ───────────────────────────────────────
    provider_choices = [PROVIDER_AWS_BEDROCK, PROVIDER_ANTHROPIC_API, PROVIDER_LOCAL]

    if non_interactive:
        provider = PROVIDER_AWS_BEDROCK
    else:
        console.print("[bold]LLM Provider:[/bold]")
        console.print(f"  [cyan]1[/cyan]. aws-bedrock (default) — Claude via AWS Bedrock")
        console.print(f"  [cyan]2[/cyan]. anthropic-api — Direct Anthropic API")
        console.print(f"  [cyan]3[/cyan]. local — Local model (Ollama, vLLM, etc.)")
        console.print()
        choice = click.prompt(
            "Select LLM provider (number or name)",
            default="1",
            type=str,
        )
        if choice.isdigit() and 1 <= int(choice) <= len(provider_choices):
            provider = provider_choices[int(choice) - 1]
        elif choice in provider_choices:
            provider = choice
        else:
            provider = PROVIDER_AWS_BEDROCK

    # ── Step 2: Provider-specific configuration ──────────────────────────────
    aws_config: dict = {}
    anthropic_config: dict = {}
    local_config: dict = {}

    if provider == PROVIDER_AWS_BEDROCK:
        # Auth method selection
        auth_choices = [AUTH_SSO_PROFILE, AUTH_ENV_VARS, AUTH_STATIC_CREDENTIALS, AUTH_INSTANCE_ROLE]

        if non_interactive:
            auth_method = AUTH_ENV_VARS
            aws_profile = ""
            aws_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        else:
            console.print("\n[bold]AWS Authentication Method:[/bold]")
            console.print(f"  [cyan]1[/cyan]. sso-profile — AWS SSO profile from ~/.aws/config")
            console.print(f"  [cyan]2[/cyan]. env-vars — AWS_ACCESS_KEY_ID etc. set at runtime")
            console.print(f"  [cyan]3[/cyan]. static-credentials — Profile from ~/.aws/credentials")
            console.print(f"  [cyan]4[/cyan]. instance-role — EC2/ECS instance role (no config needed)")
            console.print()
            auth_choice = click.prompt(
                "Select auth method (number or name)",
                default="1",
                type=str,
            )
            if auth_choice.isdigit() and 1 <= int(auth_choice) <= len(auth_choices):
                auth_method = auth_choices[int(auth_choice) - 1]
            elif auth_choice in auth_choices:
                auth_method = auth_choice
            else:
                auth_method = AUTH_SSO_PROFILE

            # Profile selection based on auth method
            if auth_method == AUTH_SSO_PROFILE:
                profiles = _detect_aws_profiles()
                if profiles:
                    console.print("\n[bold]Available AWS SSO profiles:[/bold]")
                    for i, p in enumerate(profiles, 1):
                        console.print(f"  [cyan]{i}[/cyan]. {p}")
                    console.print()
                    choice = click.prompt(
                        "Select AWS profile (number or name)",
                        default="1",
                        type=str,
                    )
                    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
                        aws_profile = profiles[int(choice) - 1]
                    elif choice in profiles:
                        aws_profile = choice
                    else:
                        aws_profile = choice
                else:
                    console.print("[yellow]No AWS profiles found in ~/.aws/config[/yellow]")
                    aws_profile = click.prompt("AWS profile name", default="")
            elif auth_method == AUTH_STATIC_CREDENTIALS:
                cred_profiles = _detect_aws_credential_profiles()
                if cred_profiles:
                    console.print("\n[bold]Available credential profiles (~/.aws/credentials):[/bold]")
                    for i, p in enumerate(cred_profiles, 1):
                        console.print(f"  [cyan]{i}[/cyan]. {p}")
                    console.print()
                    choice = click.prompt(
                        "Select credential profile (number or name)",
                        default="1",
                        type=str,
                    )
                    if choice.isdigit() and 1 <= int(choice) <= len(cred_profiles):
                        aws_profile = cred_profiles[int(choice) - 1]
                    elif choice in cred_profiles:
                        aws_profile = choice
                    else:
                        aws_profile = choice
                else:
                    console.print("[yellow]No profiles found in ~/.aws/credentials[/yellow]")
                    aws_profile = click.prompt("Credential profile name", default="default")
            elif auth_method == AUTH_ENV_VARS:
                aws_profile = ""
                console.print("\n  [dim]Note: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and optionally")
                console.print("  AWS_SESSION_TOKEN must be set in the environment at runtime.[/dim]")
            elif auth_method == AUTH_INSTANCE_ROLE:
                aws_profile = ""
                console.print("\n  [dim]Note: Using default boto3 credential chain (instance role).[/dim]")
            else:
                aws_profile = ""

            aws_region = click.prompt("AWS region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

        aws_config = {
            "profile": aws_profile,
            "region": aws_region,
            "auth_method": auth_method,
        }

        # ── Model Probe: test which models are accessible ──────────────────
        from cap.lib.model_probe import (
            create_bedrock_client,
            get_default_models_for_region,
            probe_all_models,
            region_prefix,
        )

        if non_interactive:
            # Non-interactive: use region-based defaults directly (no Bedrock calls)
            bedrock_models = get_default_models_for_region(aws_region)
            console.print(f"\n  [dim]Non-interactive: using region-based defaults for {aws_region}[/dim]")
        else:
            # Interactive: probe models to find what actually works
            console.print("\n[bold]Testing model access...[/bold]")
            try:
                bedrock_client = create_bedrock_client(
                    region=aws_region,
                    profile=aws_profile,
                    auth_method=auth_method,
                )

                def _probe_progress(model_id: str, success: bool):
                    icon = "[green]OK[/green]" if success else "[red]FAIL[/red]"
                    console.print(f"  {icon}  {model_id}")

                bedrock_models = probe_all_models(
                    client=bedrock_client,
                    region=aws_region,
                    progress_callback=_probe_progress,
                )

                if not bedrock_models:
                    console.print("  [yellow]No models responded. Using region defaults.[/yellow]")
                    bedrock_models = get_default_models_for_region(aws_region)
                else:
                    # Report assigned tiers
                    for tier, model_id in bedrock_models.items():
                        console.print(f"  [green]Assigned[/green] {tier} = {model_id}")
                    # Fill missing tiers with defaults
                    defaults = get_default_models_for_region(aws_region)
                    for tier in ("haiku", "sonnet", "opus"):
                        if tier not in bedrock_models:
                            bedrock_models[tier] = defaults[tier]
                            console.print(f"  [yellow]Fallback[/yellow] {tier} = {defaults[tier]} (not probed)")

            except Exception as exc:
                console.print(f"  [yellow]Probe failed:[/yellow] {exc}")
                console.print("  [dim]Using region-based defaults.[/dim]")
                bedrock_models = get_default_models_for_region(aws_region)

    elif provider == PROVIDER_ANTHROPIC_API:
        if non_interactive:
            api_key_env = "ANTHROPIC_API_KEY"
        else:
            console.print("\n[bold]Anthropic API Configuration:[/bold]")
            console.print("  [dim]The API key is NOT stored in config. Instead, we store the name")
            console.print("  of the environment variable that contains it (default: ANTHROPIC_API_KEY).[/dim]\n")
            api_key_env = click.prompt(
                "Environment variable name for API key",
                default="ANTHROPIC_API_KEY",
            )
            # Validate that the env var is set (warning only)
            if not os.environ.get(api_key_env):
                console.print(f"  [yellow]Warning:[/yellow] ${api_key_env} is not currently set. "
                              f"Set it before running CAP agents.")

        anthropic_config = {
            "api_key_env": api_key_env,
        }

    elif provider == PROVIDER_LOCAL:
        if non_interactive:
            local_config = {
                "base_url": "http://localhost:11434",
                "models": {"haiku": "llama3", "sonnet": "codestral", "opus": "llama3:70b"},
            }
        else:
            console.print("\n[bold]Local Model Configuration:[/bold]")
            base_url = click.prompt("Base URL", default="http://localhost:11434")
            console.print("\n  [dim]Enter model names for each tier:[/dim]")
            model_haiku = click.prompt("  Haiku-equivalent model", default="llama3")
            model_sonnet = click.prompt("  Sonnet-equivalent model", default="codestral")
            model_opus = click.prompt("  Opus-equivalent model", default="llama3:70b")
            local_config = {
                "base_url": base_url,
                "models": {"haiku": model_haiku, "sonnet": model_sonnet, "opus": model_opus},
            }

    # ── Step 3: Region (for non-bedrock, still useful for API routing) ───────
    # Already handled in the bedrock path above

    # ── Step 4: Budget ───────────────────────────────────────────────────────
    daily_budget = 5.0 if non_interactive else click.prompt("Daily budget limit (USD)", default=5.0, type=float)

    # ── Step 5: Model tier ───────────────────────────────────────────────────
    tier = "balanced" if non_interactive else click.prompt(
        "Model tier (economy=cheapest, balanced=default, quality=best)",
        type=click.Choice(["economy", "balanced", "quality"]),
        default="balanced",
    )

    # ── Step 6: Indexing paths ───────────────────────────────────────────────
    if non_interactive:
        indexing_local_paths: list[str] = []
    else:
        console.print("\n[bold]Workspace Indexing Paths:[/bold]")
        console.print("  [dim]Paths to scan for repos (comma-separated, or empty for CWD only).[/dim]")
        console.print("  [dim]Example: /Users/you/projects, /Users/you/work/infra[/dim]\n")
        paths_input = click.prompt(
            "Paths to index (comma-separated, empty=CWD only)",
            default="",
            type=str,
        )
        if paths_input.strip():
            indexing_local_paths = [
                p.strip() for p in paths_input.split(",") if p.strip()
            ]
        else:
            indexing_local_paths = []

    # ── Step 7: Remote git endpoints ─────────────────────────────────────────
    indexing_remotes: list[dict] = []
    if non_interactive:
        pass  # No remotes in non-interactive mode
    else:
        console.print("\n[bold]Remote Git Endpoints:[/bold]")
        console.print("  [dim]Configure SSH-only git remotes for auto-cloning repos.[/dim]")
        console.print("  [cyan]1[/cyan]. GitHub")
        console.print("  [cyan]2[/cyan]. Bitbucket")
        console.print("  [cyan]3[/cyan]. GitLab")
        console.print("  [cyan]4[/cyan]. None (skip)")
        console.print()

        while True:
            remote_choice = click.prompt(
                "Add remote endpoint (1-4, or 'done' to finish)",
                default="4",
                type=str,
            )
            if remote_choice in ("4", "done", ""):
                break

            if remote_choice == "1":
                remote_type = "github"
                default_ssh = "git@github.com"
            elif remote_choice == "2":
                remote_type = "bitbucket"
                default_ssh = "git@bitbucket.org"
            elif remote_choice == "3":
                remote_type = "gitlab"
                default_ssh = "git@gitlab.com"
            else:
                console.print(f"  [yellow]Invalid choice: {remote_choice}[/yellow]")
                continue

            org_label = "group" if remote_type == "gitlab" else "org"
            org_name = click.prompt(f"  {remote_type} {org_label} name", type=str)
            ssh_endpoint = click.prompt(
                f"  SSH endpoint", default=default_ssh, type=str
            )
            auto_clone = click.confirm("  Auto-clone new repos?", default=True)

            remote_entry: dict = {
                "type": remote_type,
                "ssh_endpoint": ssh_endpoint,
                "auto_clone": auto_clone,
            }
            if remote_type == "gitlab":
                remote_entry["group"] = org_name
            else:
                remote_entry["org"] = org_name

            indexing_remotes.append(remote_entry)
            console.print(f"  [green]✓[/green] Added {remote_type}/{org_name}")
            console.print()

    # ── Step 8: Embedding model preference ───────────────────────────────────
    if non_interactive:
        embedding_provider = "bedrock"
        embedding_model_id = "amazon.titan-embed-text-v2:0"
        embedding_fallback = "sentence-transformers"
        embedding_fallback_model = "all-MiniLM-L6-v2"
        embedding_dimensions = 1024
    else:
        console.print("\n[bold]Embedding Model Preference:[/bold]")
        console.print("  [cyan]1[/cyan]. titan-v2 — Amazon Titan Text Embeddings V2 (1024 dims, via Bedrock)")
        console.print("  [cyan]2[/cyan]. cohere-v3 — Cohere Embed English V3 (1024 dims, via Bedrock)")
        console.print("  [cyan]3[/cyan]. sentence-transformers — Local all-MiniLM-L6-v2 (384 dims, free)")
        console.print("  [cyan]4[/cyan]. auto — Probe Bedrock, fall back to local if unavailable")
        console.print()
        emb_choice = click.prompt(
            "Select embedding model (1-4)",
            default="4",
            type=str,
        )

        if emb_choice == "1":
            embedding_provider = "bedrock"
            embedding_model_id = "amazon.titan-embed-text-v2:0"
            embedding_dimensions = 1024
            embedding_fallback = "sentence-transformers"
            embedding_fallback_model = "all-MiniLM-L6-v2"
        elif emb_choice == "2":
            embedding_provider = "bedrock"
            embedding_model_id = "cohere.embed-english-v3"
            embedding_dimensions = 1024
            embedding_fallback = "sentence-transformers"
            embedding_fallback_model = "all-MiniLM-L6-v2"
        elif emb_choice == "3":
            embedding_provider = "sentence-transformers"
            embedding_model_id = "all-MiniLM-L6-v2"
            embedding_dimensions = 384
            embedding_fallback = "none"
            embedding_fallback_model = ""
        elif emb_choice == "4":
            embedding_provider = "bedrock"
            embedding_model_id = "amazon.titan-embed-text-v2:0"
            embedding_dimensions = 1024
            embedding_fallback = "sentence-transformers"
            embedding_fallback_model = "all-MiniLM-L6-v2"
            console.print("  [dim]Auto: will probe Bedrock at runtime, fall back to local if needed.[/dim]")
        else:
            embedding_provider = "bedrock"
            embedding_model_id = "amazon.titan-embed-text-v2:0"
            embedding_dimensions = 1024
            embedding_fallback = "sentence-transformers"
            embedding_fallback_model = "all-MiniLM-L6-v2"

    # ── Build config ─────────────────────────────────────────────────────────
    if provider == PROVIDER_AWS_BEDROCK:
        models = bedrock_models  # Set by probe or region defaults above
    else:
        models = _PROVIDER_MODEL_DEFAULTS.get(provider, {"haiku": "", "sonnet": "", "opus": ""})

    config: dict = {
        "provider": provider,
        "budget": {
            "daily_limit_usd": daily_budget,
            "alert_threshold_pct": 80,
        },
        "models": models,
        "agent_defaults": _MODEL_TIERS[tier],
        "execution": {
            "max_tool_iterations": 15,
            "max_retries": 2,
            "backoff_base_s": 1.0,
            "default_max_tokens": 8192,
            "temperature": 0.7,
        },
        "indexing": {
            "local_paths": indexing_local_paths,
            "remotes": indexing_remotes,
            "clone_base_path": str(_cap_home() / "repos"),
            "exclude_patterns": ["node_modules", ".git", "vendor", "__pycache__", ".terraform", "dist", "build"],
            "file_extensions": [".py", ".go", ".ts", ".tf", ".yaml", ".yml", ".json", ".md", ".sh", ".rs", ".hcl", ".toml", ".js"],
            "max_file_size_kb": 512,
            "reindex_interval_minutes": 360,
        },
        "embeddings": {
            "provider": embedding_provider,
            "model_id": embedding_model_id,
            "dimensions": embedding_dimensions,
            "fallback": embedding_fallback,
            "fallback_model": embedding_fallback_model,
            "region": aws_config.get("region", "eu-central-1") if provider == PROVIDER_AWS_BEDROCK else "eu-central-1",
            "profile": aws_config.get("profile") if provider == PROVIDER_AWS_BEDROCK else None,
        },
    }

    # Add provider-specific config sections
    if provider == PROVIDER_AWS_BEDROCK:
        config["aws"] = aws_config
    elif provider == PROVIDER_ANTHROPIC_API:
        config["anthropic"] = anthropic_config
    elif provider == PROVIDER_LOCAL:
        config["local"] = local_config

    # Backward compat: always include aws section (may be empty for non-bedrock)
    if "aws" not in config:
        config["aws"] = {"profile": "", "region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"), "auth_method": ""}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    # Print configuration summary
    console.print("\n[bold]Configuration Summary:[/bold]")
    console.print(f"  Provider:     [cyan]{provider}[/cyan]")
    if provider == PROVIDER_AWS_BEDROCK:
        console.print(f"  Auth Method:  [cyan]{aws_config.get('auth_method', '')}[/cyan]")
        if aws_config.get("profile"):
            console.print(f"  AWS Profile:  [cyan]{aws_config['profile']}[/cyan]")
        console.print(f"  Region:       [cyan]{aws_config.get('region', 'us-east-1')}[/cyan]")
    elif provider == PROVIDER_ANTHROPIC_API:
        console.print(f"  API Key Env:  [cyan]${anthropic_config.get('api_key_env', 'ANTHROPIC_API_KEY')}[/cyan]")
    elif provider == PROVIDER_LOCAL:
        console.print(f"  Base URL:     [cyan]{local_config.get('base_url', '')}[/cyan]")
    console.print(f"  Daily Budget: [cyan]${daily_budget:.2f}[/cyan]")
    console.print(f"  Model Tier:   [cyan]{tier}[/cyan]")
    if indexing_local_paths:
        console.print(f"  Indexing:     [cyan]{len(indexing_local_paths)} path(s)[/cyan]")
    if indexing_remotes:
        console.print(f"  Remotes:      [cyan]{len(indexing_remotes)} endpoint(s)[/cyan]")
    console.print(f"  Embeddings:   [cyan]{embedding_provider}/{embedding_model_id}[/cyan]")
    console.print(f"\n  [green]✓[/green] Configuration written to {config_path}")

    return config


# ── cap init ──────────────────────────────────────────────────────────────────

@click.command()
@click.option("--minimal", is_flag=True, help="Only install knowledge + session servers and hooks (lightweight KB-only setup)")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--skip-mcp", is_flag=True, help="Don't register MCP servers with Claude")
@click.option("--workspace", type=click.Path(), default=None, help="Workspace root (default: git root of CWD, or CWD)")
@click.option("--skip-fetch", is_flag=True, help="Don't run git fetch on detected repos")
@click.option("--non-interactive", is_flag=True, help="Use all defaults without prompting")
def init(minimal: bool, force: bool, skip_mcp: bool, workspace: str | None, skip_fetch: bool, non_interactive: bool = False):
    """Initialize the CAP platform (run after install).

    Follows the Cold Start Flow from CAP System Design v1 Section 10:
    7 phases from directory creation to health-checked readiness in ~10s.

    Use --minimal for a lightweight KB-only setup (knowledge + session servers
    and hooks only; skips orchestrator, code-intel, fleet, diagram, backlog,
    and workflow-engine).
    """
    from cap.lib.db_init import initialize_all_databases

    t_start = time.time()

    # ── Pre-flight checks ────────────────────────────────────────────────────
    py_ok, py_msg = _check_python_version()
    if not py_ok:
        console.print(f"[bold red]Error:[/bold red] {py_msg}")
        raise SystemExit(1)

    settings_warning = _warn_if_settings_missing()
    if settings_warning:
        console.print(f"[yellow]Note:[/yellow] {settings_warning}\n")

    cap_home = _cap_home()
    data_dir = cap_home / "data"
    claude_dir = _claude_dir()
    workspace_path = _resolve_workspace(workspace)

    from cap import __version__ as cap_version
    console.print(Panel(
        f"[bold]CAP — Claude Agent Platform[/bold] v{cap_version}\n"
        f"Home: {cap_home}\n"
        f"Workspace: {workspace_path}",
        box=box.ROUNDED, style="cyan",
    ))

    # ── Setup Wizard (first-time config) ────────────────────────────────────
    harness_config_path = cap_home / "harness-config.json"
    if not harness_config_path.exists() or force:
        _run_setup_wizard(force=force, non_interactive=non_interactive)

    manifest = _load_manifest(cap_home)

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 0: Create directories (CAP_HOME/data, CAP_HOME/hooks, CAP_HOME/config, CAP_HOME/backups)
    # ══════════════════════════════════════════════════════════════════════════
    console.print("\n[bold cyan]Phase 0[/bold cyan] [dim](0s)[/dim] — Creating directories")
    dirs_to_create = [
        cap_home,
        data_dir,
        cap_home / "hooks",
        cap_home / "config",
        cap_home / "backups",
        cap_home / "logs",
    ]
    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)
    console.print(f"  [green]✓[/green] {cap_home} (data, hooks, config, backups, logs)")

    # Backup existing user configs before any modifications
    backups = _backup_user_configs()
    if backups:
        for label, path in backups.items():
            console.print(f"  [green]✓[/green] Backed up {label} → {Path(path).name}")
        manifest["backups"] = backups

    # Auto-detect workspace repos
    console.print(f"\n  [bold]Workspace detection:[/bold] scanning {workspace_path} (max depth 2)")
    detected_repos = _detect_workspace_repos(workspace_path)
    if detected_repos:
        console.print(f"  [green]✓[/green] Found {len(detected_repos)} git repo(s):")
        for repo in detected_repos[:10]:
            console.print(f"      {repo.name}/")
        if len(detected_repos) > 10:
            console.print(f"      ... and {len(detected_repos) - 10} more")
    else:
        console.print(f"  [dim]─[/dim] No git repos detected in workspace")

    t_phase0 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 1 (0-2s): Create cap.db, run migrate(), write default config.toml
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 1[/bold cyan] [dim]({t_phase0:.1f}s)[/dim] — Database + config initialization")

    # Config
    config_path = cap_home / "config.toml"
    bundled = _get_bundled_data_path()

    if not config_path.exists() or force:
        src = bundled / "config.toml.default"
        if src.exists():
            shutil.copy2(src, config_path)
            console.print(f"  [green]✓[/green] config.toml written")
        else:
            config_path.write_text(
                '[platform]\n'
                'version = "0.5.0"\n'
                'log_level = "INFO"\n'
                '\n[workspace]\n'
                f'root = "{workspace_path}"\n'
                f'repos_detected = {len(detected_repos)}\n'
            )
            console.print(f"  [green]✓[/green] config.toml created (minimal)")
    else:
        console.print(f"  [dim]─[/dim] config.toml exists (use --force to overwrite)")

    # Databases
    initialize_all_databases(data_dir)
    for db_name in ["platform.db", "knowledge.db", "sessions.db", "fleet.db"]:
        db_path = data_dir / db_name
        if db_path.exists():
            os.chmod(db_path, 0o600)
    console.print(f"  [green]✓[/green] 4 databases initialized (WAL mode, 0600 permissions)")

    t_phase1 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 2 (2-3s): Generate hook scripts + update settings.json with hook entries
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 2[/bold cyan] [dim]({t_phase1:.1f}s)[/dim] — Hook generation + enforcement")

    # Generate pretool.py / posttool.py in ~/.claude/hooks/
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_generated = _generate_hook_scripts(claude_dir, data_dir)
    if hooks_generated:
        console.print(f"  [green]✓[/green] pretool.py (enforcement) generated")
        console.print(f"  [green]✓[/green] posttool.py (sync + health tracking) generated")
    else:
        console.print(f"  [dim]─[/dim] Hook scripts already exist")

    # Update settings.json with hook entries pointing to the scripts
    if _install_hooks():
        console.print(f"  [green]✓[/green] Hook entries added to settings.json (PreToolUse, PostToolUse, Stop)")
    else:
        console.print(f"  [dim]─[/dim] Hook entries already configured")

    # Copy .harness/ governance defaults to workspace
    harness_src = bundled / "harness"
    harness_dest = workspace_path / ".harness"
    if harness_src.exists():
        harness_dest.mkdir(parents=True, exist_ok=True)
        for f in harness_src.iterdir():
            if f.suffix == ".json":
                dest_file = harness_dest / f.name
                if not dest_file.exists() or force:
                    shutil.copy2(f, dest_file)
        console.print(f"  [green]✓[/green] .harness/mcp-policy.json provisioned to workspace")
    else:
        console.print(f"  [dim]─[/dim] No bundled harness defaults found")

    t_phase2 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 3 (3-5s): Quick-index + intelligent index workspace
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 3[/bold cyan] [dim]({t_phase2:.1f}s)[/dim] — Index workspace")

    if detected_repos:
        indexed_count = _quick_index_workspace(data_dir, workspace_path, detected_repos)
        console.print(f"  [green]✓[/green] {indexed_count} files quick-indexed into FTS5 (README, manifests, configs)")
    else:
        # Index CWD directly if no repos detected
        indexed_count = _quick_index_workspace(data_dir, workspace_path, [workspace_path])
        if indexed_count > 0:
            console.print(f"  [green]✓[/green] {indexed_count} files quick-indexed from CWD")
        else:
            console.print(f"  [dim]─[/dim] No indexable files found in workspace (quick-index)")

    from cap.lib.harness_config import add_indexed_root, get_knowledge_config, get_indexing_config

    knowledge_config = get_knowledge_config()

    def _progress_cb(msg: str, done: int, total: int) -> None:
        if total > 0:
            pct = int(done / total * 100) if total > 0 else 0
            console.print(f"  [dim]...[/dim] {msg} ({pct}%)")
        else:
            console.print(f"  [dim]...[/dim] {msg}")

    # Intelligent indexing via IntelligentIndexer — discovery + understanding pipeline.
    # Falls back to recursive_indexer when IntelligentIndexer is unavailable or Bedrock
    # is unreachable (skip_llm_analysis=True ensures no hard Bedrock dependency at init).
    intelligent_stats: dict = {"files_indexed": 0}
    try:
        from cap.lib.intelligent_indexer import IntelligentIndexer, IndexerConfig

        indexing_cfg = get_indexing_config()
        local_paths: list[str] = indexing_cfg.get("local_paths", [])
        if not local_paths:
            local_paths = [str(workspace_path)]

        total_paths = len(local_paths)
        completed_paths = 0

        def _indexer_progress(msg: str, phase_cur: int, phase_tot: int) -> None:
            console.print(f"  [dim]...[/dim] Analyzing repos... ({completed_paths}/{total_paths} complete) — {msg}")

        indexer_config = IndexerConfig(
            workspace_roots=local_paths,
            full_reindex=force,
            # Disable LLM analysis during init — it adds latency and requires
            # Bedrock credentials that may not be configured yet.  The daemon
            # will run a full analysis on the next scheduled reindex.
            skip_llm_analysis=True,
            skip_embedding=True,  # embeddings are handled by the daemon
            incremental=not force,
        )
        indexer = IntelligentIndexer(indexer_config)
        try:
            indexer_run_stats = indexer.run_sync(progress_callback=_indexer_progress)
            intelligent_stats["files_indexed"] = indexer_run_stats.files_indexed
            intelligent_stats["repos_discovered"] = indexer_run_stats.repos_discovered
            intelligent_stats["graph_nodes_created"] = indexer_run_stats.graph_nodes_created
            console.print(
                f"  [green]✓[/green] Intelligent index: {indexer_run_stats.files_indexed} files, "
                f"{indexer_run_stats.repos_discovered} repos, "
                f"{indexer_run_stats.graph_nodes_created} graph nodes"
            )
            if indexer_run_stats.errors:
                console.print(
                    f"  [yellow]![/yellow] {len(indexer_run_stats.errors)} non-fatal indexing error(s) — "
                    "run `cap sync` for details"
                )
        finally:
            indexer.close()

        add_indexed_root(str(workspace_path))
        console.print(f"  [green]✓[/green] Workspace root added to knowledge.indexed_roots")

    except Exception as exc:
        console.print(f"  [yellow]![/yellow] Intelligent indexing failed: {exc} — falling back to recursive indexer")
        # Fall back to the legacy recursive indexer so init never leaves the
        # knowledge base completely empty.
        try:
            from cap.lib.recursive_indexer import index_directory_tree

            recursive_config = {
                "data_dir": str(data_dir),
                "extensions": set(knowledge_config.get("file_extensions", [])),
                "exclude_dirs": set(knowledge_config.get("exclude_patterns", [])),
                "max_file_size_kb": knowledge_config.get("max_file_size_kb", 500),
                "batch_size": 100,
                "workspace": str(workspace_path),
            }
            recursive_stats = index_directory_tree(
                root=workspace_path,
                config=recursive_config,
                progress_callback=_progress_cb,
            )
            intelligent_stats["files_indexed"] = recursive_stats.get("files_indexed", 0)
            console.print(
                f"  [green]✓[/green] Fallback recursive index: {intelligent_stats['files_indexed']} files indexed"
            )
            try:
                add_indexed_root(str(workspace_path))
            except Exception:
                pass
        except Exception as fallback_exc:
            console.print(f"  [yellow]![/yellow] Fallback indexing also failed: {fallback_exc}")
            console.print(f"  [dim]─[/dim] Quick-index results remain available")

    # Add intelligent index count to total
    indexed_count += intelligent_stats.get("files_indexed", 0)

    # Load baseline corrections
    corrections_loaded = _load_baseline_corrections(data_dir, str(workspace_path))
    if corrections_loaded > 0:
        console.print(f"  [green]✓[/green] {corrections_loaded} baseline corrections loaded")
    else:
        console.print(f"  [dim]─[/dim] Baseline corrections already present")

    # Load routing seed patterns for cold-start bootstrap
    console.print(f"  [dim]...[/dim] Loading routing seed patterns...")
    seed_count = _load_routing_seed_patterns(data_dir, force=force)
    if seed_count > 0:
        console.print(f"  [green]✓[/green] {seed_count} routing seed patterns loaded")
    else:
        console.print(f"  [dim]─[/dim] Routing seed patterns already present")

    t_phase3 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 4 (5-10s): Register MCP servers + install agents/workflows/permissions
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 4[/bold cyan] [dim]({t_phase3:.1f}s)[/dim] — MCP servers + agents + permissions")

    # Agents
    if not minimal:
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
            console.print(f"  [green]✓[/green] {len(installed_agents)} agents installed")
        else:
            console.print(f"  [yellow]![/yellow] No bundled agents found")
        manifest["installed_agents"] = installed_agents

        # Workflows
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
            console.print(f"  [green]✓[/green] {len(installed_workflows)} workflows installed")
        else:
            console.print(f"  [yellow]![/yellow] No bundled workflows found")
        manifest["installed_workflows"] = installed_workflows
    else:
        console.print(f"  [dim]─[/dim] Agents/workflows skipped (--minimal)")

    # CLAUDE.md
    if _install_claude_md(force):
        console.print(f"  [green]✓[/green] ~/.claude/CLAUDE.md installed")
    else:
        console.print(f"  [dim]─[/dim] CLAUDE.md exists (use --force)")

    # Settings permissions
    if _install_settings_permissions():
        console.print(f"  [green]✓[/green] {len(_CAP_MCP_PERMISSIONS)} tool permissions → settings.json")
    else:
        console.print(f"  [dim]─[/dim] Permissions already configured")

    # MCP Servers (cap-knowledge, cap-session, cap-orchestrator, cap-code-intel, cap-backlog)
    if not skip_mcp:
        # Platform servers (AWS, K8s, Terraform)
        platform_servers = _get_platform_mcp_servers()
        platform_registered = 0
        for srv in platform_servers:
            if _mcp_server_exists(srv["name"]):
                continue
            cmd_args = ["add", srv["name"], "--scope", "user"]
            for var in srv["env"]:
                cmd_args.extend(["-e", var])
            cmd_args.extend(["--", srv["command"]] + srv["args"])
            if _run_claude_mcp(cmd_args):
                platform_registered += 1
        if platform_registered > 0:
            console.print(f"  [green]✓[/green] {platform_registered} platform MCP servers registered")

        # CAP servers — in minimal mode only install knowledge + session
        cap_servers = _get_cap_mcp_servers(cap_home, data_dir)
        if minimal:
            cap_servers = _filter_cap_servers_minimal(cap_servers)
        registered = []
        for srv in cap_servers:
            if force:
                _run_claude_mcp(["remove", "--scope", "user", srv["name"]])
            cmd_args = ["add", srv["name"], "--scope", "user"]
            for var in srv["env"]:
                cmd_args.extend(["-e", var])
            cmd_args.extend(["--", str(srv["command"])] + srv["args"])
            if _run_claude_mcp(cmd_args):
                registered.append(srv["name"])
        console.print(f"  [green]✓[/green] {len(registered)} CAP MCP servers registered")
        if len(registered) < len(cap_servers):
            failed = len(cap_servers) - len(registered)
            console.print(f"  [yellow]![/yellow] {failed} server(s) failed — register manually with `claude mcp add`")
        manifest["mcp_servers"] = registered
    else:
        console.print(f"  [dim]─[/dim] MCP servers skipped (--skip-mcp)")

    t_phase4 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 5 (background): Queue full AST index + git fetch
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 5[/bold cyan] [dim]({t_phase4:.1f}s)[/dim] — Background tasks (non-blocking)")

    # Queue AST index for next session_start
    ast_queue_path = cap_home / "data" / ".ast_index_pending"
    if detected_repos:
        ast_queue_path.write_text(json.dumps({
            "workspace": str(workspace_path),
            "repos": [str(r) for r in detected_repos],
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }))
        console.print(f"  [green]✓[/green] Full AST index queued for next session_start ({len(detected_repos)} repos)")
    else:
        console.print(f"  [dim]─[/dim] No repos to queue for AST indexing")

    # Git fetch --all on detected repos (if network available)
    if not skip_fetch and detected_repos:
        console.print(f"  [dim]...[/dim] Running git fetch --all on {len(detected_repos)} repo(s)...")
        fetch_results = _git_fetch_repos(detected_repos)
        fetched_ok = sum(1 for v in fetch_results.values() if v)
        fetched_fail = sum(1 for v in fetch_results.values() if not v)
        if fetched_ok > 0:
            console.print(f"  [green]✓[/green] git fetch succeeded on {fetched_ok} repo(s)")
        if fetched_fail > 0:
            console.print(f"  [yellow]![/yellow] git fetch failed on {fetched_fail} repo(s) (network unavailable?)")
    elif skip_fetch:
        console.print(f"  [dim]─[/dim] git fetch skipped (--skip-fetch)")

    t_phase5 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 6: Health check — verify DB, hooks syntax, MCP server configs
    # ══════════════════════════════════════════════════════════════════════════
    console.print(f"\n[bold cyan]Phase 6[/bold cyan] [dim]({t_phase5:.1f}s)[/dim] — Health check")

    health_results = _run_health_check(cap_home, data_dir, claude_dir)
    passed = sum(1 for _, ok, _ in health_results if ok)
    failed = sum(1 for _, ok, _ in health_results if not ok)

    if failed == 0:
        console.print(f"  [green]✓[/green] All {passed} checks passed")
    else:
        console.print(f"  [green]✓[/green] {passed} checks passed")
        console.print(f"  [yellow]![/yellow] {failed} checks failed:")
        for name, ok, detail in health_results:
            if not ok:
                console.print(f"      [red]✗[/red] {name}: {detail}")

    t_phase6 = time.time() - t_start

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 7: Save manifest + print success summary
    # ══════════════════════════════════════════════════════════════════════════

    # Save manifest
    manifest["version"] = cap_version
    manifest["cap_home"] = str(cap_home)
    manifest["python"] = sys.executable
    manifest["workspace"] = str(workspace_path)
    manifest["repos_detected"] = len(detected_repos)
    manifest["installed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["init_duration_s"] = round(t_phase6, 2)
    _save_manifest(cap_home, manifest)

    t_total = time.time() - t_start

    # Summary
    console.print(f"\n[bold cyan]Phase 7[/bold cyan] [dim]({t_phase6:.1f}s)[/dim] — Complete")
    console.print("\n" + "─" * 60)

    # ── Post-init self-check ─────────────────────────────────────────────────
    console.print("\n[bold]Post-init verification[/bold]")
    verify_rows = _run_post_init_verification(data_dir)
    verify_table = Table(box=box.SIMPLE_HEAD, show_edge=False, show_header=True)
    verify_table.add_column("Check", style="bold")
    verify_table.add_column("Status", justify="center")
    verify_table.add_column("Detail", style="dim")
    for check, status, detail in verify_rows:
        if status == "yes":
            status_str = "[green]yes[/green]"
        elif status == "no":
            status_str = "[red]no[/red]"
        else:
            status_str = f"[cyan]{status}[/cyan]"
        verify_table.add_row(check, status_str, detail)
    console.print(verify_table)

    # ── Shell restart hint (only if cap not yet on PATH) ─────────────────────
    needs_restart = not _cap_on_path()

    summary_lines = [
        "[bold green]CAP initialized successfully![/bold green]",
        f"  Total time: {t_total:.1f}s | Repos: {len(detected_repos)} | Files indexed: {indexed_count}",
        f"  Mode: {'minimal (KB-only)' if minimal else 'full'}",
        "",
        "[bold]What's ready now:[/bold]",
        "  - FTS5 search across workspace README/manifests/configs",
        "  - Hook enforcement active (delegation reminders)",
        "  - Session memory + corrections loaded",
        f"  - {len(manifest.get('mcp_servers', []))} MCP servers registered",
        "",
        "[bold]Queued for next session:[/bold]",
        "  - Full AST index (tree-sitter parse of all source files)",
        "  - Relationship graph (calls, imports, extends)",
        "",
        "[bold]Next steps:[/bold]",
        "  [cyan]cap status[/cyan]              — Platform overview",
        "  [cyan]cap doctor[/cyan]              — Detailed health check",
        "  [cyan]cap sync[/cyan]                — Trigger full workspace sync now",
        "",
        "[bold]In Claude Code:[/bold]",
        "  Reload window (Cmd+Shift+P → Reload) to pick up new MCP servers.",
        "  Then ask anything — Claude will use knowledge_search automatically.",
    ]

    if needs_restart:
        summary_lines += [
            "",
            "[bold yellow]Shell restart required:[/bold yellow]",
            "  `cap` was not found on PATH. Restart your shell (or run",
            "  `source ~/.zshrc` / `source ~/.bashrc`) so `cap` is available.",
        ]

    summary_lines += [
        "",
        "[bold]Safety:[/bold]",
        f"  Config backups stored in {_backups_dir()}",
        "  Run [cyan]cap uninstall[/cyan] to cleanly restore original configs.",
    ]

    console.print(Panel(
        "\n".join(summary_lines),
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
        console.print(f"[yellow]CAP is not installed (no {cap_home} found).[/yellow]")
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
    if claude_md.exists() and claude_md.read_text().startswith(_CLAUDE_MD_FALLBACK[:120]):
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

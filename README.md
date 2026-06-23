# Claude Agent Platform

A self-improving multi-agent system for [Claude Code CLI](https://claude.ai/code). Turns Claude into a team of 14 autonomous specialists that coordinate, review each other's work, and learn from every session.

## What You Get

- **14 specialist agents** — devops, security, architect, SRE, dev, optimization, CI/CD, testing, code review, docs, teacher, system (self-improving)
- **10 multi-agent workflows** — new service deployment, incident response, security audit, cost optimization, architecture explainer, and more
- **Proactive session observer** — automatically captures your corrections and preferences, improves with every conversation
- **Knowledge base** — persistent memory across all sessions and workspaces
- **9 MCP servers** — structured read-only access to AWS (IAM, EKS, CloudWatch, Lambda, pricing), Terraform registry, and Kubernetes
- **Auto-sync** — repos stay fresh, stale branches get pruned
- **Self-improving** — a dedicated system agent audits and optimizes the setup itself

## Prerequisites

- **Claude Code CLI** installed and authenticated ([install guide](https://claude.ai/code))
- **git**, **node/npm**, **python3** (most systems have these)
- **uvx** (optional, for AWS MCP servers) — install with `pip install uv`
- **AWS CLI + SSO** (optional, for AWS access) — configured in `~/.aws/config`

## Install

```bash
git clone <this-repo> claude-agent-platform
cd claude-agent-platform
./install.sh
```

The installer asks a few questions:
1. AI provider (Anthropic direct / AWS Bedrock / skip)
2. AWS SSO session name (optional — enables 7 AWS MCP servers)
3. Git conventions (SSH vs HTTPS, branch pattern)
4. Effort level (how deep Claude thinks)

Then it installs everything to `~/.claude/` (global, works in any directory).

## Usage

Just use Claude normally. The system routes automatically:

```bash
# In any project directory:
claude

# Security audit (spawns security agent, 3 parallel dimensions)
> audit this repo for security issues

# New service (triggers full workflow: architect → implement → review → monitor → document)
> deploy a new Lambda that processes SQS events

# Incident response (triggers workflow: triage → correlate → mitigate → fix → postmortem)  
> production alerts firing, pods OOMKilling in staging

# Architecture explanation (parallel mappers + synthesizer)
> how does the deployment pipeline work end-to-end?

# Cost optimization (3-way scan → alternatives → implement)
> our AWS bill is too high, find waste

# Self-improvement
> improve the agent system
> add a new agent for database migrations
```

## Commands

| Command | What it does |
|---------|--------------|
| `~/.claude/scripts/init-repo.sh [path]` | Set up a new repo with Claude integration |
| `~/.claude/scripts/aws-sso-login.sh` | Authenticate AWS SSO (all accounts) |
| `~/.claude/scripts/aws-sso-login.sh status` | Check SSO session health |
| `~/.claude/scripts/auto-sync.sh` | Fetch/pull all repos in workspace |
| `~/.claude/scripts/build-knowledge-base.sh` | Rebuild repo index in knowledge base |

## Inside Claude, say:

| You say | What happens |
|---------|--------------|
| `/session-observe` | Analyze session, save learnings |
| `/repo-sync-clean` | Sync repos + prune stale branches |
| "improve the setup" | System agent audits and fixes |
| "review this code" | Code-review + security agents run |
| "explain how X works" | Teacher agent with architecture context |

## How It Works

### Agent Coordination

```
You → Orchestrator → routes to specialist(s)
                   → parallel: security + code-review
                   → sequential: architect → implementer
                   → workflow: multi-phase pipeline
```

Agents pass context to each other. The orchestrator manages handoffs, retry loops, and conflict resolution (security has veto power).

### Self-Improvement Loop

```
Session → Observer captures corrections/preferences → Memory
        → System agent detects patterns → Updates agents/workflows
        → Knowledge base grows → Future sessions start smarter
```

### File Layout

```
~/.claude/
├── CLAUDE.md           ← Orchestrator instructions
├── settings.json       ← Permissions, model, env
├── agents/             ← 14 specialist agent definitions
├── workflows/          ← 10 multi-agent pipeline scripts
├── scripts/            ← Utility scripts (sync, init, SSO)
└── knowledge/          ← Persistent brain (repos, domains, tasks)
```

## Upgrade

```bash
cd claude-agent-platform
git pull
./upgrade.sh    # Updates agents, workflows, scripts — preserves your config
```

## Uninstall

```bash
./uninstall.sh  # Removes everything except knowledge base
```

## Customization

### Add a new agent

Create `~/.claude/agents/my-agent.md`:
```markdown
---
name: my-agent
description: What this agent does (shown in picker menu)
model: sonnet
---

# My Agent

You are a specialist in X. Your responsibilities:
- Do Y
- Validate Z
```

Then tell Claude: "add my-agent to the routing table" — the system agent will update CLAUDE.md.

### Add a new workflow

Create `~/.claude/workflows/my-workflow.js`:
```javascript
export const meta = {
  name: 'my-workflow',
  description: 'What it does',
  whenToUse: 'When to trigger it',
  phases: [
    { title: 'Phase1', detail: 'What happens' },
    { title: 'Phase2', detail: 'What happens next' },
  ],
}

phase('Phase1')
const result = await agent('Do X', { agentType: 'devops' })

phase('Phase2')
await agent(`Based on ${result}, do Y`, { agentType: 'security' })
```

### Modify permissions

Edit `~/.claude/settings.json` — add patterns to `allow` or `deny`.

## Architecture Decisions

- **Global install (`~/.claude/`)** — works identically in every directory without per-project setup
- **Read-only MCP servers** — all AWS access is read-only by design; no server can create/modify/delete
- **Security veto** — security agent can block merges; its concerns must be addressed before proceeding
- **Knowledge over re-reading** — knowledge base is checked first; raw files only when KB isn't enough
- **One SSO login = all accounts** — a single browser auth grants access to the entire organization
- **Proactive learning** — corrections are captured silently; the system never needs the same feedback twice

## License

MIT

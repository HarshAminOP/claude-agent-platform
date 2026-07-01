# Claude Agent Platform (CAP)

A multi-agent orchestration platform for Claude Code that adds persistent memory, codebase intelligence, budget controls, and live workflow visibility.

```
                         +-------------------+
                         |   Claude Code     |
                         +--------+----------+
                                  |
                         +--------v----------+
                         |   CAP MCP Layer   |
                         |  (9 core servers) |
                         +--------+----------+
                                  |
          +----------+------------+------------+----------+
          |          |            |            |          |
     +----v---+ +---v----+ +----v----+ +-----v----+ +---v------+
     |Knowledge| |Session | |Workflow | |Orchestr. | |Backlog   |
     |Search   | |Memory  | |Engine   | |+ Router  | |+ Decisions|
     +----+----+ +--------+ +---------+ +----+-----+ +----------+
          |                                   |
     +----v----+                         +----v-----+
     | SQLite  |                         | Bedrock  |
     | + FTS5  |                         | Anthropic|
     | + Vector|                         | Local    |
     +---------+                         +----------+
```

## Quick Start

```bash
pip install claude-agent-platform   # or: uv tool install claude-agent-platform
cap init                            # configure provider, models, budget
cap status                          # verify all systems running
```

After `cap init`, CAP is active in every Claude Code session. No further setup needed.

## Features

| Feature | Description |
|:--------|:------------|
| **Persistent Memory** | Corrections, decisions, and learnings survive across sessions |
| **Codebase Intelligence** | Hybrid search (keyword + semantic + knowledge graph) across all repos |
| **Multi-Agent Orchestration** | 139 specialist agents routed by complexity tier |
| **Budget Controls** | Monthly caps, per-task limits, automatic kill on overspend |
| **Live Workflow Visibility** | Real-time agent collaboration rendered as TUI |
| **Self-Healing Infrastructure** | 9 MCP servers with health checks and auto-restart |
| **Task Queue** | Structured backlog with dependencies and acceptance criteria |
| **Decision Protocol** | Options with tradeoffs presented to PO for resolution |
| **Progressive Autonomy** | Earned trust per (agent, action) pair |
| **Blast Radius Analysis** | Pre-execution dependency traversal via knowledge graph |
| **Terraform Drift Detection** | Auto-detect and backlog infrastructure drift |
| **AST Code Search** | Pattern-based structural search via ast-grep |

## LLM Providers

CAP supports three provider backends:

| Provider | Auth | Best For |
|:---------|:-----|:---------|
| **AWS Bedrock** | SSO profile, env vars, instance role | Teams with AWS infrastructure |
| **Anthropic API** | `ANTHROPIC_API_KEY` env var | Individual developers |
| **Local (Ollama)** | None | Offline use, experimentation |

Configure during `cap init` or change later with `cap config set provider <name>`.

## CLI Overview

```
cap init              Set up databases, config, MCP servers
cap status            System health overview
cap config show|set   View/modify configuration
cap knowledge search  Search across indexed codebases
cap session list      View session history
cap workflow list     List active workflows
cap budget status     Cost tracking and limits
cap sync              Index workspace into knowledge base
cap github config     Configure org and auto-resolution
cap doctor            Diagnose common issues
cap uninstall         Clean removal with config restore
```

See [docs/cli-reference.md](docs/cli-reference.md) for complete command documentation.

## Documentation

| Guide | Description |
|:------|:------------|
| [Installation](docs/installation.md) | Prerequisites, install, provider setup |
| [Configuration](docs/configuration.md) | Complete config reference |
| [Agents](docs/agents.md) | Agent catalog and routing |
| [CLI Reference](docs/cli-reference.md) | Every command with usage and examples |
| [Architecture](docs/architecture.md) | System design and execution flows |
| [Providers: Bedrock](docs/providers/bedrock.md) | AWS Bedrock setup |
| [Providers: Anthropic](docs/providers/anthropic.md) | Direct API setup |
| [Providers: Local](docs/providers/local.md) | Ollama/local models |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and fixes |

## Requirements

- Python 3.11+
- Claude Code installed
- One of: AWS credentials (Bedrock), Anthropic API key, or Ollama (local)

## License

MIT

<div align="center">

<br>

# Claude Agent Platform

### Your AI assistant just became an engineering team.

<br>

CAP transforms Claude from a single chat session into a **persistent, budget-aware, multi-specialist platform** that remembers everything, searches your entire codebase, and coordinates work like a real team — visible in real time.

<br>

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.3.0-blue?style=for-the-badge)](pyproject.toml)

<br>

[Why CAP](#why-cap) · [Features](#features) · [Get Started](#get-started) · [Documentation](#documentation)

<br>

</div>

---

<br>

## Why CAP

Every AI coding assistant has the same problem:

> It forgets everything the moment you close the window.

You spend time teaching it your architecture, correcting its mistakes, explaining your conventions — and next session, it's a blank slate again. Multiply that across a team of engineers, each re-teaching the same AI the same things, and you're bleeding hours every week.

**CAP solves this permanently.**

| Capability | Without CAP | With CAP |
|:-----------|:---:|:---:|
| Remembers across sessions | ✗ | ✓ |
| Searches your actual codebase | ✗ | ✓ |
| Live visibility into multi-agent work | ✗ | ✓ |
| Budget controls & automatic kill | ✗ | ✓ |
| Self-healing infrastructure | ✗ | ✓ |
| Quality evaluation framework | ✗ | ✓ |
| One-command install & uninstall | ✗ | ✓ |

<br>

---

<br>

## Features

<br>

### Persistent Memory

Your AI learns once and remembers forever.

When you correct Claude — "don't use mocks for DB tests, we got burned by that" — CAP stores that correction with confidence scoring. Next session, next week, next month: it remembers. Decisions, preferences, patterns, domain knowledge — all persisted and reinforced over time.

The more you use it, the smarter it gets. Not through retraining — through accumulated institutional knowledge that never fades.

<br>

---

<br>

### Codebase Intelligence

Ask anything about your code. Get answers from reality, not imagination.

CAP continuously indexes your workspace using three search channels working together:

- **Keyword matching** — fast exact lookups (like grep, but ranked)
- **Semantic search** — finds conceptually related content even without keyword overlap
- **Knowledge graph** — traverses relationships between services, configs, and decisions

Results are automatically fused and ranked. Claude cites where it found each answer. No more "I think the config is probably in..." — it knows exactly where it is.

<br>

---

<br>

### Live Team Simulation

Watch your AI work like a coordinated engineering team — not a black box.

When CAP handles complex tasks, it assigns work to specialist agents (Architect, DevOps, Security, SRE, etc.) and renders their collaboration as a live conversation:

```
┌─ Architecture ───────────────────────────────────────────
│
│  Architect    Proposed: ALB → EKS, 3 replicas, Karpenter nodes
│  Security  →  Architect  "IAM role too broad — needs source conditions"
│  Architect    Acknowledged. Revising to least-privilege...
│  Architect  ✓ Design approved (2 revisions)
│
┌─ Implementation ─────────────────────────────────────────
│
│  DevOps      Writing Terraform + Helm chart
│  SRE         Creating alerting rules
│  DevOps   →  SRE  "Chart ready. Need alerts for p99 > 500ms"
│  SRE       ✓ 3 alerts: latency, error_rate, saturation
│  DevOps    ✓ Plan: +14 resources, no destroy
│
└─ Done ───────────────────────────────────────────────────
   2m 34s · $0.42 · 6 agents · passed
```

You see concerns raised, handoffs between specialists, acknowledgements, and final decisions — all in real time. Not after the fact. Not in a log file. **As it happens.**

<br>

---

<br>

### Budget Controls

Set it and forget it. Or watch it like a hawk. Either way, you're in control.

- **Monthly caps** — hard ceiling on total spend
- **Per-task limits** — each workflow has its own budget
- **Automatic kill** — exceeds budget? Workflow stops immediately
- **Live tracking** — see exactly what each model tier costs

No more "I left it running overnight and it burned $200." CAP makes that impossible.

<br>

---

<br>

### Self-Managing Infrastructure

CAP runs 4 background servers that extend Claude's capabilities. You never need to think about them:

- Automatic health checks every 30 seconds
- Self-restart on failure with exponential backoff
- Zero-touch setup — one command brings everything online
- Clean shutdown and restore on uninstall

If something fails, it fixes itself. If something can't be fixed, it tells you why.

<br>

---

<br>

## What's Included

| | Component | What It Does |
|:--|:----------|:-------------|
| **Agents** | 14 specialists | Architect, DevOps, Security, SRE, CI/CD, Test, Optimization, Code Review, Docs, and more |
| **Workflows** | 10 pipelines | New service setup, incident response, security audit, cost optimization, architecture review |
| **Servers** | 4 MCP servers | Knowledge retrieval, session memory, workflow orchestration, fleet health |
| **CLI** | `cap` command | Status, diagnostics, eval, workflow watch, budget tracking, knowledge search |
| **Eval** | Quality framework | Automated scoring of retrieval accuracy, security coverage, session memory, and workflows |

<br>

---

<br>

## Get Started

Four commands. Under 60 seconds.

```bash
uv tool install claude-agent-platform    # Install the package
uv tool update-shell && source ~/.zshrc  # Add cap to your PATH (one-time)
cap init                                  # Set up databases, agents, servers
cap status                                # Verify everything is running
```

**Done.** Next time you open Claude Code, CAP is active. No configuration needed.

<br>

> **Zero risk:** CAP backs up your existing settings before touching anything. Uninstall restores them perfectly — your system returns to exactly how it was before.

<br>

<details>
<summary><strong>What about my existing Claude setup?</strong></summary>

<br>

CAP is additive. It doesn't replace or modify your existing Claude Code configuration — it extends it by registering additional MCP servers. Your current agents, settings, and workflows remain untouched.

If you uninstall, `cap uninstall` removes only what it added and restores your original configs from backup.

</details>

<details>
<summary><strong>How does Claude Code connect to LLMs?</strong></summary>

<br>

CAP works with however you've already set up Claude Code:

- **Anthropic API** (default) — just `claude auth login`
- **AWS Bedrock** — set `CLAUDE_CODE_USE_BEDROCK=1` + your AWS SSO profile
- **Google Vertex AI** — set `CLAUDE_CODE_USE_VERTEX=1` + GCP config

CAP doesn't make its own LLM calls — it orchestrates Claude Code sessions that use whichever provider you configured. No extra LLM setup needed.

</details>

<details>
<summary><strong>Do I need AWS credentials?</strong></summary>

<br>

**Only for semantic search (optional).** CAP uses Amazon Titan Text Embeddings V2 to generate vectors for semantic search. Without it, CAP uses keyword search + knowledge graph — which covers most use cases well.

You can install and use CAP fully without AWS credentials. If you later want semantic search, configure Bedrock access and run `cap sync --full`. Alternative embedding providers (OpenAI, local models) are planned.

</details>

<details>
<summary><strong>Can I share this with my team?</strong></summary>

<br>

Yes. Build a wheel with `uv build` and share the `.whl` file, or install directly from your Git repository:

```bash
uv tool install git+ssh://git@github.com/your-org/claude-agent-platform.git
```

See the [Distribution Guide](docs/DISTRIBUTION.md) for all options.

</details>

<br>

---

<br>

## Who It's For

| Role | What CAP gives you |
|:-----|:-------------------|
| **Any engineer using Claude** | It remembers your preferences and never repeats the same mistake twice |
| **Platform / DevOps** | Instant search across dozens of repos, infrastructure decisions persisted |
| **Tech leads** | Visibility into AI-assisted work, cost controls, quality scoring |
| **Teams** | Shared knowledge base that compounds with every conversation |

<br>

---

<br>

## Safe By Design

| Principle | How |
|:----------|:----|
| **Your data stays local** | Everything in SQLite on your machine. Nothing sent anywhere except optional AWS embeddings. |
| **Non-destructive install** | Backs up configs before modifying. Uninstall restores originals. |
| **Budget-enforced** | Monthly caps, per-workflow limits, auto-kill. Impossible to overspend silently. |
| **Secure by default** | Database files `0600`. Path traversal blocked. Injection patterns detected. Command whitelist enforced. |

<br>

---

<br>

## Documentation

| Guide | Description |
|:------|:------------|
| **[Installation](docs/INSTALL.md)** | Prerequisites, setup, credentials, troubleshooting |
| **[Usage](docs/USAGE.md)** | How and when each feature activates, with examples |
| **[Configuration](docs/CONFIGURATION.md)** | Every setting explained — budgets, search weights, sync behavior |
| **[Distribution](docs/DISTRIBUTION.md)** | Build, publish, and share with your team |
| **[Technical](docs/TECHNICAL.md)** | Architecture diagrams, API surface, internals |
| **[Architecture](docs/ARCHITECTURE.md)** | System design document |
| **[ADRs](docs/adr/)** | Key technical decisions with rationale |

<br>

---

<br>

<div align="center">

**One install. Permanent memory. Real-time visibility. Full control.**

<br>

```bash
uv tool install claude-agent-platform && cap init
```

<br>

MIT License · [MOIA Platform Engineering](https://github.com/moia-dev)

</div>

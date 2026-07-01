# Installation

## Prerequisites

### Python 3.11+

CAP requires Python 3.11, 3.12, or 3.13.

```bash
python3 --version
```

### uv (recommended) or pip

```bash
# macOS
brew install uv

# Linux / macOS (standalone)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Claude Code

CAP extends Claude Code via MCP servers. Install Claude Code first:

```bash
claude --version
```

## Install CAP

### From PyPI

```bash
uv tool install claude-agent-platform
uv tool update-shell && source ~/.zshrc  # one-time PATH update
```

### From source

```bash
git clone git@github.com:your-org/claude-agent-platform.git
cd claude-agent-platform
uv tool install .
```

### From wheel

```bash
uv tool install ./dist/claude_agent_platform-2.0.0-py3-none-any.whl
```

## Initialize

### Interactive (default)

```bash
cap init
```

The wizard prompts for:
1. **Provider** -- aws-bedrock, anthropic-api, or local
2. **Authentication** -- SSO profile, env vars, static credentials, or instance role
3. **Region** -- AWS region (for Bedrock)
4. **Budget** -- Daily spend limit in USD
5. **Model tier** -- standard, performance, or budget

### Non-interactive

```bash
cap init --non-interactive
```

Uses defaults:
- Provider: aws-bedrock
- Auth: env-vars (reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- Region: from `AWS_DEFAULT_REGION` env var (falls back to us-east-1)
- Budget: $5.00/day
- Tier: standard

### Minimal (databases only)

```bash
cap init --minimal
```

Creates databases and config without registering MCP servers or installing agents.

## Provider-Specific Setup

### AWS Bedrock

```bash
cap init
# Select: aws-bedrock
# Select auth method (SSO recommended)
# Enter profile name and region
```

Requirements:
- AWS SSO profile or credentials configured
- Bedrock model access enabled in your account
- See [providers/bedrock.md](providers/bedrock.md) for IAM policy details

### Anthropic API

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
cap init
# Select: anthropic-api
```

Requirements:
- Valid Anthropic API key
- See [providers/anthropic.md](providers/anthropic.md) for details

### Local (Ollama)

```bash
ollama pull llama3
cap init
# Select: local
# Enter Ollama base URL (default: http://localhost:11434)
```

Requirements:
- Ollama installed and running
- At least one model pulled
- See [providers/local.md](providers/local.md) for compatible models

## Verifying Installation

```bash
cap status
```

Expected output:
```
CAP Status: HEALTHY
  Databases:     OK (cap.db, platform.db)
  MCP Servers:   9/9 registered
  Knowledge:     indexed (N files, M edges)
  Budget:        $0.00 / $5.00 today
  Provider:      aws-bedrock (us-east-1)
```

## Troubleshooting Install

| Symptom | Fix |
|:--------|:----|
| `cap: command not found` | Run `uv tool update-shell && source ~/.zshrc` |
| MCP servers not registered | Run `cap init --force` |
| Database errors | Run `cap db-doctor --fix` |
| Provider auth fails | Run `cap doctor` for diagnostics |

## Uninstall

```bash
cap uninstall           # removes config + servers, restores backups
cap uninstall --keep-data  # keeps databases (knowledge, sessions)
```

CAP backs up your original Claude Code settings before modifying them. Uninstall restores the originals.

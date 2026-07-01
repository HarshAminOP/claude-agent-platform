# Anthropic API Provider

## Overview

Direct access to Claude models via the Anthropic Messages API. Best for individual developers or teams without AWS infrastructure.

## Setup

### 1. Get an API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to API Keys
3. Create a new key
4. Copy the key (starts with `sk-ant-`)

### 2. Set Environment Variable

```bash
export ANTHROPIC_API_KEY="sk-ant-api03-..."
```

Add to your shell profile (`~/.zshrc`, `~/.bashrc`) for persistence:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-api03-..."' >> ~/.zshrc
```

### 3. Initialize CAP

```bash
cap init
# Select: anthropic-api
# Confirm API key env var name (default: ANTHROPIC_API_KEY)
```

## Configuration

In `~/.claude-platform/harness-config.json`:

```json
{
  "provider": "anthropic-api",
  "anthropic": {
    "api_key_env": "ANTHROPIC_API_KEY"
  }
}
```

The API key is NEVER stored in config files. Only the environment variable name is stored.

### Custom Environment Variable

If you use a different env var name:

```json
{
  "anthropic": {
    "api_key_env": "MY_CLAUDE_KEY"
  }
}
```

## Available Models

| Tier | Model ID | Use Case |
|:-----|:---------|:---------|
| haiku | claude-haiku-4-5-20251001 | Fast tasks, docs, simple lookups |
| sonnet | claude-sonnet-4-6 | General development, standard tasks |
| opus | claude-opus-4-8 | Complex architecture, security review |

Models are automatically selected based on agent tier configuration.

## Rate Limits

Anthropic applies rate limits per API key:

| Limit Type | Free Tier | Scale Tier |
|:-----------|:----------|:-----------|
| Requests/min | 50 | 4,000 |
| Input tokens/min | 40,000 | 400,000 |
| Output tokens/min | 8,000 | 80,000 |

CAP handles rate limiting automatically with exponential backoff (configurable in `execution.max_retries` and `execution.backoff_base_s`).

## Cost

Pricing (per 1M tokens):

| Model | Input | Output |
|:------|:------|:-------|
| Haiku | $0.80 | $4.00 |
| Sonnet | $3.00 | $15.00 |
| Opus | $15.00 | $75.00 |

Set daily limits via:
```bash
cap config set daily_budget_usd 10.0
```

## Limitations vs. Bedrock

| Feature | Anthropic API | AWS Bedrock |
|:--------|:--------------|:------------|
| Semantic search (embeddings) | Not supported | Titan V2 included |
| Auth method | API key | SSO/IAM/instance role |
| Billing | Anthropic billing | AWS billing |
| VPC access | Public internet | VPC endpoints available |
| Audit trail | API logs | CloudTrail |

When using the Anthropic API provider, semantic search is unavailable. CAP uses keyword + knowledge graph search instead (retrieval weights auto-adjust).

## Troubleshooting

| Error | Cause | Fix |
|:------|:------|:----|
| `AuthenticationError` | Invalid or missing key | Check `ANTHROPIC_API_KEY` is set correctly |
| `RateLimitError` | Too many requests | CAP auto-retries; reduce concurrency if persistent |
| `OverloadedError` | API overloaded | Transient; CAP retries automatically |
| `InvalidRequestError` | Malformed request | Check model ID is valid |
| Key not found | Env var not set | Set `ANTHROPIC_API_KEY` in shell profile |

### Anthropic API Provider

Direct API access. Uses `langchain-anthropic` ChatAnthropic.

#### Setup

1. Set environment variable:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

2. Configure provider in harness-config.json:
```json
{
  "provider": "anthropic-api",
  "anthropic": {
    "api_key_env": "ANTHROPIC_API_KEY"
  }
}
```

Or select during `cap init` wizard.

#### Available Models

| Tier | Model ID | Notes |
|------|----------|-------|
| haiku | claude-haiku-4-5-20250501 | Fast, cheap |
| sonnet | claude-sonnet-4-5-20250514 | Balanced |
| opus | claude-opus-4-20250514 | Most capable |

#### Embedding

Anthropic does not provide an embedding API. Options:
1. **Voyage AI** (recommended): set up separately if available
2. **sentence-transformers** (default fallback): `all-MiniLM-L6-v2`, runs locally, 384 dimensions

Configure fallback in harness-config.json:
```json
{
  "embeddings": {
    "fallback": "sentence-transformers"
  }
}
```

#### When to Use

- No AWS account available
- Development/testing environments
- Personal projects with API key
- When Bedrock model access is restricted by SCP

#### Limitations vs Bedrock

- No native embedding model (falls back to local)
- API key management (env var, not IAM)
- No SSO integration
- Rate limits are per-key (not per-account)

#### Cross-links
Link to: [Configuration](../configuration.md), [Bedrock Provider](bedrock.md)

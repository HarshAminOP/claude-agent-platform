### AWS Bedrock Provider

Default provider. Uses `langchain-aws` ChatBedrockConverse.

#### Available Models

| Tier | Model ID | Use Case |
|------|----------|----------|
| haiku | eu.anthropic.claude-haiku-4-5 | Fast lookups, docs, simple tasks |
| sonnet | eu.anthropic.claude-sonnet-4-5 | Code implementation, reviews, testing |
| opus | us.anthropic.claude-opus-4-5 | Architecture, security, complex analysis |

Note: Exact model IDs depend on region and account access. The `cap init` wizard probes available models.

#### Authentication Methods

| Method | Config Key | Description |
|--------|-----------|-------------|
| sso-profile | `aws.auth_method: "sso-profile"` | AWS SSO, passes `credentials_profile_name` |
| env-vars | `aws.auth_method: "env-vars"` | Uses AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY |
| instance-role | `aws.auth_method: "instance-role"` | EC2/ECS instance role, boto3 default chain |

#### Region Configuration

Set in `harness-config.json`:
```json
{
  "aws": {
    "region": "eu-central-1",
    "profile": "<your-profile>"
  }
}
```

Or via CLI: `cap config set bedrock.region eu-west-1`

#### Model Probe During Setup

During `cap init`, CAP probes which models are accessible:
- Sends a minimal request to each model tier
- Reports success/failure per model
- Falls back to available models if some are blocked
- Results stored in harness-config.json under `models`

#### SCP Considerations

Some organizations have Service Control Policies that block certain models:
- Cohere models: commonly blocked
- Titan models: typically available (used for embeddings)
- Anthropic models: usually available but may be region-restricted

If a model probe fails, check:
1. SCP policies in your AWS organization
2. Model access requests in Bedrock console (must be explicitly enabled)
3. Region availability (not all models in all regions)

#### Embedding: Titan Embed V2

Default embedding model: `amazon.titan-embed-text-v2:0`
- Dimensions: 1024
- Max input: 8192 tokens
- Batch size: 25 texts per API call
- Max concurrent: 3 (configurable)

Configure in config.toml:
```toml
[bedrock]
embedding_model = "amazon.titan-embed-text-v2:0"
embedding_dimensions = 1024
embedding_max_concurrent = 3
```

#### Retry Configuration

```toml
[bedrock.retry]
max_retries = 3
base_delay_ms = 500
max_delay_ms = 10000
backoff_multiplier = 2.0
```

Exponential backoff with jitter. Handles ThrottlingException and transient 5xx errors.

#### Cross-links
Link to: [Configuration](../configuration.md), [Troubleshooting](../troubleshooting.md)

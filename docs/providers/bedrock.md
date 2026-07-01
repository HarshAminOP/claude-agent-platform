# AWS Bedrock Provider

## Overview

CAP uses AWS Bedrock to access Claude models (Haiku, Sonnet, Opus) via cross-region inference profiles. This is the recommended provider for teams with existing AWS infrastructure.

## IAM Requirements

### Minimum Policy

The IAM principal (user/role) needs the following permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/*anthropic.claude-*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2*"
      ],
      "Sid": "EmbeddingsOptional"
    }
  ]
}
```

The embeddings permission is optional. Without it, CAP falls back to keyword + graph search (no semantic search).

### Model Access

Bedrock models must be enabled in your account:

1. Go to AWS Console > Bedrock > Model access
2. Enable access for:
   - Anthropic Claude (all tiers you want to use)
   - Amazon Titan Text Embeddings V2 (for semantic search)
3. Wait for access to be granted (usually instant, sometimes up to 24h)

## Authentication Methods

### SSO Profile (recommended)

```bash
# In ~/.aws/config:
[profile my-bedrock-profile]
sso_session = my-sso
sso_account_id = 123456789012
sso_role_name = BedrockAccess
region = us-east-1

[sso-session my-sso]
sso_start_url = https://my-org.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access

# Login:
aws sso login --sso-session my-sso

# cap init selects this profile
```

### Environment Variables

```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."  # if using temporary credentials
export AWS_DEFAULT_REGION="us-east-1"
```

### Static Credentials

```bash
# In ~/.aws/credentials:
[my-profile]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```

### Instance Role

For EC2/ECS/Lambda -- no configuration needed. boto3 discovers credentials automatically from the instance metadata service.

## Region Selection

CAP uses cross-region inference profiles. The region prefix is derived automatically:

| AWS Region | Prefix | Example Model ID |
|:-----------|:-------|:-----------------|
| `us-east-1`, `us-west-2` | `us` | `us.anthropic.claude-sonnet-4-6` |
| `eu-central-1`, `eu-west-1` | `eu` | `eu.anthropic.claude-sonnet-4-6` |
| `ap-northeast-1`, `ap-southeast-1` | `ap` | `ap.anthropic.claude-sonnet-4-6` |

Choose a region where Bedrock Claude models are available. Check [AWS Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) for model availability by region.

## Model Probe

During `cap init`, CAP probes Bedrock to verify which models are accessible:

```
Probing models in us-east-1...
  haiku: us.anthropic.claude-haiku-4-5-20251001-v1:0  [OK]
  sonnet: us.anthropic.claude-sonnet-4-6              [OK]
  opus: us.anthropic.claude-opus-4-8                  [OK]
```

If a model is not accessible, CAP falls back to the next candidate in the tier.

## Service Control Policies (SCPs)

If your organization uses SCPs, ensure they allow:
- `bedrock:InvokeModel` on Claude and Titan models
- Cross-region inference profile invocations

Common SCP issues:
- Region restrictions blocking cross-region profiles
- Model-level denylists blocking specific Claude versions
- Account-level Bedrock disablement

## Cost

Model pricing (per 1M tokens):

| Model | Input | Output |
|:------|:------|:-------|
| Haiku | $0.80 | $4.00 |
| Sonnet | $3.00 | $15.00 |
| Opus | $15.00 | $75.00 |
| Titan Embed V2 | $0.02 | -- |

CAP tracks costs automatically. Use `cap budget status` to monitor.

## Troubleshooting

| Error | Cause | Fix |
|:------|:------|:----|
| `AccessDeniedException` | IAM policy missing | Add bedrock:InvokeModel permission |
| `ResourceNotFoundException` | Model not enabled | Enable model in Bedrock console |
| `ThrottlingException` | Rate limit hit | CAP auto-retries with backoff |
| `ExpiredTokenException` | SSO session expired | Run `aws sso login` |
| `NoCredentialsError` | No credentials found | Check profile/env vars |

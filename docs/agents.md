# Agents

CAP ships 139 specialist agent definitions. The orchestrator routes tasks to agents based on complexity tier and domain.

## Core Agents (21 primary)

These are the agents registered by `cap init` and used by the orchestrator for delegation:

| Agent | Model Tier | Domain | Description |
|:------|:-----------|:-------|:------------|
| `orchestrator` | opus | coordination | Task decomposition and multi-agent coordination |
| `aws-architect` | opus | architecture | AWS architecture design and review |
| `security` | opus | security | Security analysis, threat modeling, IAM review |
| `code-review` | opus | quality | Multi-dimensional code review |
| `system` | opus | platform | System-level decisions and platform management |
| `dev` | sonnet | development | General software development |
| `devops` | sonnet | infrastructure | Terraform, CI/CD, deployment |
| `sre` | sonnet | reliability | Alerting, runbooks, incident response |
| `cicd` | sonnet | deployment | Pipeline design and troubleshooting |
| `test` | sonnet | testing | Test strategy and generation |
| `explore` | sonnet | research | Code search and exploration |
| `docs` | haiku | documentation | Documentation writing |
| `optimization` | haiku | performance | Cost and performance optimization |
| `teacher` | sonnet | education | Explanations and onboarding |
| `workflow` | sonnet | orchestration | Workflow definition and management |
| `data` | sonnet | data | Data engineering and pipelines |
| `frontend` | sonnet | ui | Frontend development |

## Extended Agent Catalog (139 total)

Agents are grouped by domain. Each has a markdown definition in `src/cap/data/agents/`.

### Infrastructure & Cloud

| Agent | Purpose |
|:------|:--------|
| `eks-cluster` | EKS cluster setup and management |
| `eks-addons` | EKS addon configuration |
| `eks-node-groups` | Node group sizing and configuration |
| `karpenter` | Karpenter autoscaler setup |
| `vpc-network` | VPC design and subnet layout |
| `subnet-design` | Subnet CIDR planning |
| `alb-ingress` | ALB/Ingress controller setup |
| `nlb-loadbalancer` | NLB configuration |
| `route53-dns` | DNS management |
| `cloudfront-cdn` | CDN configuration |
| `s3-buckets` | S3 bucket policies and lifecycle |
| `rds-aurora` | RDS/Aurora database setup |
| `dynamodb` | DynamoDB table design |
| `lambda-functions` | Lambda function development |
| `step-functions` | Step Functions workflows |
| `eventbridge` | EventBridge rules |
| `sqs-queues` | SQS queue configuration |
| `sns-topics` | SNS topic setup |
| `ecr-registry` | ECR container registry |
| `ecs-fargate` | ECS Fargate services |
| `parameter-store` | SSM Parameter Store |
| `secrets-manager` | Secrets Manager rotation |

### Security & Compliance

| Agent | Purpose |
|:------|:--------|
| `iam-policy-review` | IAM policy analysis |
| `least-privilege-audit` | Least privilege assessment |
| `network-security` | Security group and NACL review |
| `waf-rules` | WAF rule configuration |
| `encryption-review` | Encryption-at-rest/transit review |
| `secret-rotation` | Secret rotation automation |
| `container-scanning` | Container vulnerability scanning |
| `penetration-test` | Penetration test planning |
| `threat-model` | Threat modeling |
| `forensics` | Security forensics |
| `zero-trust` | Zero trust architecture |
| `compliance-gdpr` | GDPR compliance review |
| `compliance-hipaa` | HIPAA compliance review |
| `compliance-sox` | SOX compliance review |

### CI/CD & Deployment

| Agent | Purpose |
|:------|:--------|
| `github-actions` | GitHub Actions workflow design |
| `gitlab-ci` | GitLab CI configuration |
| `jenkins-pipeline` | Jenkins pipeline design |
| `argocd-apps` | ArgoCD application setup |
| `argocd-sync` | ArgoCD sync strategy |
| `helm-charts` | Helm chart development |
| `helm-values` | Helm values management |
| `gitops-patterns` | GitOps workflow patterns |
| `blue-green-deploy` | Blue/green deployment |
| `canary-deploy` | Canary deployment |
| `rollback-strategy` | Rollback procedures |
| `environment-promotion` | Environment promotion flows |
| `release-management` | Release management |
| `artifact-management` | Build artifact management |
| `build-optimization` | Build speed optimization |

### Observability

| Agent | Purpose |
|:------|:--------|
| `prometheus-metrics` | Prometheus metric design |
| `grafana-dashboards` | Grafana dashboard creation |
| `alertmanager-rules` | AlertManager rule writing |
| `distributed-tracing` | Distributed tracing setup |
| `log-aggregation` | Log aggregation strategy |
| `logging-structured` | Structured logging patterns |
| `error-tracking` | Error tracking integration |
| `apm-setup` | APM configuration |
| `synthetic-monitoring` | Synthetic monitoring |
| `cost-monitoring` | Cloud cost monitoring |

### Testing

| Agent | Purpose |
|:------|:--------|
| `unit-test-python` | Python unit test generation |
| `unit-test-typescript` | TypeScript unit tests |
| `integration-test` | Integration test design |
| `e2e-test` | End-to-end test automation |
| `load-test` | Load testing (k6, Locust) |
| `chaos-test` | Chaos engineering |
| `contract-test` | Contract testing (Pact) |
| `mutation-test` | Mutation testing |
| `fuzz-test` | Fuzz testing |
| `snapshot-test` | Snapshot testing |
| `visual-regression` | Visual regression testing |
| `accessibility-test` | Accessibility testing |
| `mock-service` | Mock service creation |
| `test-data-generation` | Test data generation |
| `test-infrastructure` | Test infrastructure setup |
| `ab-testing` | A/B test design |

### Development

| Agent | Purpose |
|:------|:--------|
| `python-backend` | Python backend development |
| `typescript-frontend` | TypeScript frontend |
| `react-components` | React component design |
| `api-design` | API design (REST/GraphQL) |
| `api-contract` | API contract definition |
| `api-docs` | API documentation |
| `graphql-schema` | GraphQL schema design |
| `grpc-services` | gRPC service definition |
| `rest-endpoints` | REST endpoint implementation |
| `websocket-handlers` | WebSocket handler design |
| `database-migrations` | Database migration scripts |
| `database` | Database schema design |
| `orm-models` | ORM model definition |
| `error-handling` | Error handling patterns |
| `caching-strategy` | Caching strategy design |
| `sdk-client` | SDK client generation |
| `sdk-developer` | SDK development |
| `cli-tools` | CLI tool development |
| `algorithm` | Algorithm design |

### Data & Analytics

| Agent | Purpose |
|:------|:--------|
| `data-pipeline` | Data pipeline design |
| `data-lake` | Data lake architecture |
| `data-warehouse` | Data warehouse design |
| `data-quality` | Data quality checks |
| `data-governance` | Data governance policies |
| `etl-transform` | ETL transformation logic |
| `batch-processing` | Batch processing design |
| `streaming-kafka` | Kafka streaming setup |
| `schema-registry` | Schema registry management |
| `analytics-dashboard` | Analytics dashboard design |

### Documentation & Process

| Agent | Purpose |
|:------|:--------|
| `adr-writer` | Architecture Decision Records |
| `onboarding-guide` | Onboarding documentation |
| `runbook-author` | Runbook writing |
| `changelog-generator` | Changelog generation |
| `pr-reviewer` | Pull request review |
| `scrum-master` | Agile process guidance |
| `dependency-audit` | Dependency audit |
| `dependency-updates` | Dependency update management |

## Routing Logic

The orchestrator uses a 3-tier complexity router:

```
Task Input
    |
    v
+-------------------+
| Complexity Router |
| (keyword + model) |
+-------------------+
    |         |         |
    v         v         v
 INLINE   LIGHTWEIGHT   FULL
 (haiku)   (sonnet)    (opus)
```

**INLINE** -- Simple lookups, status checks, 1-line fixes. Handled directly without delegation.

**LIGHTWEIGHT** -- Single-agent tasks requiring one specialist. Routed to the best-matching agent (dev, devops, sre, etc.).

**FULL** -- Multi-agent tasks requiring coordination. The orchestrator decomposes into a TaskDAG and dispatches to multiple specialists.

### Agent Selection Factors

1. **Task keywords** -- domain keywords map to agent types
2. **Complexity signals** -- file count, cross-repo references, security implications
3. **Past performance** -- routing decisions are recorded and used for self-learning
4. **Budget constraints** -- cheaper tiers preferred when task permits

## Adding Custom Agents

Create a markdown file in `~/.claude/agents/` or in your project's `.claude/agents/` directory:

```markdown
---
name: my-custom-agent
model: sonnet
domain: custom
description: My custom specialist agent
---

# My Custom Agent

## Role
You are a specialist in [domain].

## Instructions
- Always [specific behavior]
- Never [constraint]

## Output Format
Return results as [format].
```

Custom agents are discovered automatically by the orchestrator. Project-level agents (in `.claude/agents/`) take precedence over global agents for work within that project.

### Agent Definition Fields

| Field | Required | Description |
|:------|:---------|:------------|
| `name` | yes | Unique identifier |
| `model` | yes | Model tier: `opus`, `sonnet`, or `haiku` |
| `domain` | yes | Domain category for routing |
| `description` | yes | One-line description |

The body of the markdown becomes the agent's system prompt.

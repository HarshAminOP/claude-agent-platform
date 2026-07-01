# Agents

CAP ships 139 specialist agents organized by domain. The orchestrator routes tasks to agents based on complexity tier and domain expertise. Each agent has a dedicated system prompt, access controls, and output contract.

## Core Engineering Agents (10)

These agents form the primary delegation tier and are used by the orchestrator for all major task categories:

| Agent | Model | Description | Use When |
|:------|:------|:------------|:---------|
| `orchestrator` | opus | Task decomposition and multi-agent coordination | You have a complex, multi-step task requiring orchestration across domains |
| `dev` | sonnet | General software development implementation | Writing application code, fixing bugs, refactoring, migrations |
| `devops` | sonnet | Infrastructure automation with Terraform/Kubernetes | Infrastructure changes, deployment configs, GitOps workflows |
| `security` | opus | Security analysis, threat modeling, IAM policy review | Security audits, threat models, policy analysis, compliance reviews |
| `sre` | sonnet | Observability, alerting, incident response, reliability | Runbooks, SLO/SLI design, alert rules, reliability patterns |
| `docs` | haiku | Documentation writing and maintenance | READMEs, ADRs, runbooks, API docs, onboarding guides |
| `test` | sonnet | Test strategy, generation, and quality gates | Test design, coverage analysis, test data generation |
| `code-review` | opus | Multi-dimensional code review for quality and security | Pull request review, pre-push validation, design review |
| `optimization` | haiku | Cost and performance optimization analysis | Right-sizing, cost waste identification, performance profiling |
| `explore` | sonnet | Code search and repository exploration | Finding files, tracing symbols, understanding codebase structure |

## AWS Infrastructure Agents (23)

Cloud platform services and infrastructure management:

| Agent | Purpose |
|:------|:--------|
| `aws-architect` | AWS architecture design, Well-Architected reviews, service selection, multi-account strategy |
| `eks-cluster` | EKS cluster lifecycle management, version upgrades, control plane config |
| `eks-addons` | EKS addon selection, versioning, and custom configuration |
| `eks-node-groups` | Node group sizing, AMI selection, GPU workload configuration |
| `ecs-fargate` | ECS Fargate service design, task definitions, auto-scaling, Spot Fargate |
| `karpenter` | Karpenter NodePool design, Spot/on-demand strategy, consolidation |
| `lambda-functions` | Lambda design, packaging, layers, event mapping, power tuning, SnapStart |
| `rds-aurora` | RDS/Aurora cluster design, failover, Multi-AZ, Serverless v2, blue-green upgrades |
| `s3-buckets` | S3 policies, lifecycle rules, CRR, versioning, Object Lock, encryption |
| `vpc-network` | VPC design, CIDR planning, Transit Gateway, PrivateLink, secondary CIDR |
| `subnet-design` | Subnet allocation for public/private/isolated tiers, AZ distribution, EKS sizing |
| `alb-ingress` | ALB/Ingress controller setup, target groups, health checks, listener rules |
| `nlb-loadbalancer` | NLB configuration for TCP/UDP/gRPC, TLS passthrough, static IPs |
| `ecr-registry` | ECR repository lifecycle policies, vulnerability scanning, cross-account pull |
| `cloudfront-cdn` | CloudFront distribution design, S3/ALB origins, Lambda@Edge, WAF integration |
| `route53-dns` | Route 53 zone management, routing policies, health checks, external-dns integration |
| `waf-rules` | WAF rule group design, rate limiting, geo-blocking, managed rules |
| `eventbridge` | EventBridge buses, event patterns, Pipes for enrichment, schema registry |
| `sqs-queues` | SQS queue design (standard/FIFO), visibility timeout, DLQ, large message handling |
| `sns-topics` | SNS topic design (standard/FIFO), subscription filters, fan-out patterns, encryption |
| `step-functions` | Step Functions state machines, Catch/Retry, Express vs Standard, SDK integration |
| `parameter-store` | Parameter Store hierarchy, SecureString encryption, GetParametersByPath policies |
| `secrets-manager` | Secrets Manager lifecycle, rotation Lambda, External Secrets Operator integration |

## CI/CD and GitOps Agents (14)

Deployment automation and release management:

| Agent | Purpose |
|:------|:--------|
| `cicd` | CI/CD pipeline design and troubleshooting across platforms |
| `github-actions` | GitHub Actions workflow design, reusable workflows, matrix builds, OIDC |
| `argocd-apps` | ArgoCD Application/ApplicationSet configuration for GitOps delivery |
| `argocd-sync` | ArgoCD sync behavior, sync waves, resource hooks, sync windows |
| `gitops-patterns` | GitOps patterns: app-of-apps, ApplicationSets, drift detection, multi-cluster |
| `gitlab-ci` | GitLab CI/CD pipeline design with stages, caching, deployments |
| `jenkins-pipeline` | Jenkins declarative pipelines, shared libraries, Kubernetes pod agents |
| `artifact-management` | Artifact tagging strategy, multi-arch manifests, cosign signing, SBOM, promotion |
| `environment-promotion` | Environment promotion workflows, ArgoCD Image Updater, Helm overlays |
| `blue-green-deploy` | Blue/green deployment design, target group switching, smoke test gates |
| `canary-deploy` | Canary deployments with Argo Rollouts, Flagger, traffic splitting, automated rollback |
| `release-management` | SemVer, Conventional Commits, Changesets, CHANGELOG automation, GitHub Releases |
| `build-optimization` | Docker BuildKit caching, multi-stage builds, parallel tests, incremental compilation |
| `monorepo-management` | Monorepo tooling (Turborepo, Nx), affected commands, selective CI, remote caching |

## Data and Streaming Agents (13)

Data engineering and stream processing:

| Agent | Purpose |
|:------|:--------|
| `data-pipeline` | Data pipeline design with Airflow, Step Functions, Glue, DAG design, SLA monitoring |
| `data-lake` | Data lake architecture on S3 with Apache Iceberg, medallion design, partitioning |
| `data-warehouse` | Data warehouse schema design (Redshift, BigQuery, Snowflake), query optimization |
| `data-quality` | Data quality frameworks with Great Expectations, dbt tests, anomaly detection |
| `data-governance` | Data governance with Lake Formation, Glue Catalog, OpenLineage, GDPR retention |
| `streaming-kafka` | Kafka/MSK design, exactly-once semantics, schema registry, consumer lag monitoring |
| `batch-processing` | Batch job design on EMR, Spark, Glue; cluster sizing, Spot strategy, shuffle tuning |
| `etl-transform` | ETL logic implementation with pandas, PySpark, dbt; null handling, deduplication |
| `schema-registry` | Schema lifecycle management, compatibility enforcement, safe evolution (Avro, Protobuf) |
| `dynamodb` | DynamoDB design, single-table patterns, PK/SK selection, GSI/LSI, access patterns |
| `database` | Schema design (relational + NoSQL), migration strategies, blue-green migrations |
| `database-migrations` | Zero-downtime migrations with Alembic, Flyway, expand-contract pattern, CONCURRENTLY |
| `orm-models` | ORM modeling with SQLAlchemy 2.0, Prisma, TypeORM; N+1 prevention, soft deletes |

## Observability Agents (10)

Monitoring, logging, tracing, and dashboards:

| Agent | Purpose |
|:------|:--------|
| `grafana-dashboards` | Grafana dashboard design using PromQL/Loki queries, templates, dashboard-as-code |
| `prometheus-metrics` | Prometheus metric design, naming, instrumentation, recording rules, PromQL |
| `log-aggregation` | Log aggregation with Fluent Bit DaemonSets, CloudWatch Logs, OpenSearch, enrichment |
| `logging-structured` | Structured JSON logging, correlation ID propagation, PII redaction, Insights queries |
| `distributed-tracing` | OpenTelemetry instrumentation, W3C TraceContext, sampling, OTEL Collector pipeline |
| `apm-setup` | APM agent configuration (Elastic, Datadog, X-Ray), auto-instrumentation, service maps |
| `synthetic-monitoring` | CloudWatch Synthetics canaries, health endpoint implementation, SSL expiry checks |
| `error-tracking` | Sentry integration, DSN setup, fingerprint rules, performance monitoring, source maps |
| `cost-monitoring` | Cloud cost monitoring with Cost Explorer, anomaly detection, tagging strategy, FinOps |
| `alertmanager-rules` | Prometheus alert rules, Alertmanager routing trees, PagerDuty integration, alert fatigue |

## Testing Agents (16)

Test design, automation, and quality assurance:

| Agent | Purpose |
|:------|:--------|
| `unit-test-python` | Python unit test generation with pytest, fixtures, parametrize, mocking |
| `unit-test-typescript` | TypeScript unit tests with Jest/Vitest, mocking, spies, coverage analysis |
| `integration-test` | Integration test design with Testcontainers, docker-compose, real dependencies |
| `e2e-test` | End-to-end test automation with Playwright/Cypress, page object model |
| `load-test` | Load testing with k6, Locust, Artillery; latency baselines, bottleneck identification |
| `fuzz-test` | Fuzz testing with Hypothesis, fast-check, Go native fuzzing, AFL++ |
| `contract-test` | Consumer-driven contract tests with Pact broker, provider verification, can-i-deploy |
| `mutation-test` | Mutation testing with Stryker/mutmut for measuring test suite quality |
| `snapshot-test` | Jest/Vitest snapshot tests, inline vs external, serializers, update workflows |
| `test-data-generation` | Test data factories with factory_boy, Fishery, faker.js; deterministic seeding |
| `test-infrastructure` | Test infrastructure optimization, CI parallelism, flaky test management |
| `visual-regression` | Visual regression testing with Chromatic TurboSnap, Percy, baseline management |
| `chaos-test` | Chaos engineering with AWS FIS, Litmus, game day runbooks, steady-state hypothesis |
| `mock-service` | Mock services with WireMock, MSW, LocalStack; request matching, fault injection |
| `ab-testing` | A/B test design, statistical significance, experiment lifecycle management |
| `accessibility-test` | WCAG 2.1 AA testing with axe-core, Pa11y, keyboard navigation, ARIA validation |

## Application Development Agents (19)

Language-specific and feature development:

| Agent | Purpose |
|:------|:--------|
| `python-backend` | Python service development with FastAPI/Flask, async patterns, Pydantic models |
| `typescript-frontend` | TypeScript frontend development with React, Next.js, Vite, strict typing |
| `react-components` | React component design with hooks, state management, render optimization |
| `api-design` | API design (REST/GraphQL) with OpenAPI 3.1, URL naming, backward compatibility |
| `api-docs` | OpenAPI 3.1 specs, AsyncAPI schemas, Swagger UI/Redoc, error catalogs, SDK generation |
| `api-contract` | API contract validation, protobuf/gRPC schema design, backward compatibility checking |
| `rest-endpoints` | REST endpoint implementation with Pydantic/Zod validation, RFC 7807 errors, pagination |
| `graphql-schema` | GraphQL schema design with type systems, DataLoader, Apollo Federation, subscriptions |
| `grpc-services` | gRPC services with protobuf, unary/streaming RPCs, interceptors, buf CLI, grpc-gateway |
| `websocket-handlers` | WebSocket servers with pub/sub via Redis, horizontal scaling, JWT auth, rate limiting |
| `cli-tools` | CLI development with Click/Cobra/Commander, subcommand design, completion, distribution |
| `sdk-developer` | SDK and library design, pagination, authentication, retry handling, plugin systems |
| `sdk-client` | SDK client generation and design for internal/external APIs |
| `error-handling` | Error handling patterns, circuit breakers, retry with jitter, bulkhead isolation, fallbacks |
| `caching-strategy` | Multi-layer caching with Redis/Valkey, CDN TTL, cache-aside patterns, stampede prevention |
| `feature-flags` | Feature flags with LaunchDarkly/Unleash/AppConfig, kill switches, percentage rollout |
| `queue-workers` | Message queue consumers, idempotency keys, at-least-once delivery, graceful shutdown |
| `algorithm` | Algorithm and data structures, complexity analysis, performance optimization |
| `dependency-updates` | Dependency update automation with Renovate, Dependabot, major version migrations |

## Security and Compliance Agents (18)

Security hardening and compliance management:

| Agent | Purpose |
|:------|:--------|
| `iam-policy-review` | IAM policy analysis for least privilege violations, dangerous patterns |
| `network-security` | Security group and NACL review, VPC endpoints, network segmentation |
| `threat-model` | Threat modeling with STRIDE and attack trees, mitigating controls (pytm, OWASP TD) |
| `penetration-test` | Penetration testing per OWASP Testing Guide, CVSS scoring, remediation reports |
| `incident-response` | Incident response playbooks, security investigation, post-mortem facilitation |
| `incident-forensics` | Security incident forensics, evidence preservation, timeline reconstruction, IOCs, MITRE ATT&CK |
| `forensics` | Digital forensics, cloud evidence preservation, log analysis, chain of custody |
| `encryption-review` | Encryption-at-rest/transit audit, KMS key policies, certificate management, envelope patterns |
| `least-privilege-audit` | Systematic IAM audit for least privilege using Access Analyzer and CloudTrail |
| `compliance-gdpr` | GDPR compliance controls, PII mapping, retention automation, right-to-erasure, consent |
| `compliance-hipaa` | HIPAA Technical Safeguards in AWS, PHI encryption, access controls, BAA alignment |
| `compliance-sox` | SOX IT General Controls, change management, access certification, audit trails |
| `container-scanning` | Container vulnerability scanning, Dockerfile hardening, image baseline policies, SBOMs |
| `dependency-audit` | Dependency audit for CVEs, license violations, supply chain risks (Go/Python/Node) |
| `secret-rotation` | Zero-downtime secret rotation for RDS, API keys, Kubernetes secrets |
| `zero-trust` | Zero-trust architecture: mTLS with SPIFFE/SPIRE, Istio policies, workload identity, microsegmentation |
| `vulnerability-scanning` | Vulnerability discovery and remediation workflows across infrastructure and code |
| `security-hardening` | Infrastructure security hardening, IAM optimization, network defense, secrets management |

## Platform and Documentation Agents (10)

Meta-tasks, documentation, and process:

| Agent | Purpose |
|:------|:--------|
| `adr-writer` | Architecture Decision Records in MADR format with drivers, options, consequences, links |
| `onboarding-guide` | Developer onboarding docs with exact versions, architecture, first-week checklist, FAQ |
| `runbook-author` | Operational runbooks with severity classification, step-by-step commands, rollback, escalation |
| `changelog-generator` | Changelog generation from Conventional Commits via git-cliff or semantic-release |
| `pr-reviewer` | Pre-push diff reviewer, automatically invoked before pushes/PRs |
| `scrum-master` | Agile process guidance, quality gates, completeness verification before delivery |
| `capacity-planning` | Capacity modeling, HPA/KEDA threshold design, resource forecasting |
| `rollback-strategy` | Rollback automation design, ArgoCD rollback, Helm rollback, feature flags, MTTR |
| `system-design` | Distributed systems design, scalability analysis, consistency models, data modeling |
| `teacher` | Platform architecture explanations, learning paths, guided onboarding, concept walkthroughs |

## Agent Lifecycle

All agents follow a consistent lifecycle:

```
┌─────────────────────────────────────────────────────────────┐
│  Task Input (from user or orchestrator)                     │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│  1. SPAWN                                                   │
│  - CoordinationEngine creates agent instance                │
│  - Model tier assigned (haiku/sonnet/opus)                  │
│  - Resource budget allocated                                │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│  2. CONTEXT INJECTION                                       │
│  - Agent system prompt loaded                               │
│  - Relevant predecessor outputs provided                    │
│  - Shared state (task context, shared files)                │
│  - Tool restrictions applied                                │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│  3. EXECUTE                                                 │
│  - LLM call via ConverseExecutor                            │
│  - System prompt + task description                         │
│  - Iterative tool use (read, bash, edit, write)             │
│  - Cost tracking per call                                   │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│  4. PUBLISH                                                 │
│  - Results written to SharedState (key-value store)         │
│  - Findings published to AgentBus (pub/sub)                 │
│  - Cost finalized and budgeted                              │
│  - Output contract validated                                │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────┐
│  5. TERMINATE                                               │
│  - Resources released (temp files, sockets)                 │
│  - Session recorded in agentdb for replay/audit             │
│  - State persisted for potential resumption                 │
│  - Metrics updated (duration, cost, success/failure)        │
└─────────────────────────────────────────────────────────────┘
```

## Agent Coordination

Agents coordinate via multiple patterns:

### AgentBus (Pub/Sub)

Agents publish findings to topics and subscribe to results from other agents:

```
Agent A publishes:
  Topic: "security/findings"
  { severity: "high", category: "iam", message: "..." }

Agent B subscribes to "security/*" and processes findings
```

Topic format: `domain/subtype` with wildcard subscriptions supported (e.g., `security/*`).

### Shared State (Key-Value)

Coordinated agents share task-scoped state:

```
SharedState
  ├─ "target_files" → [list of files being worked on]
  ├─ "deployment_plan" → [execution steps]
  ├─ "cost_estimate" → { compute: $X, storage: $Y }
  └─ "approval_gates" → [pending decisions]
```

### Request/Response (Synchronous)

When one agent needs immediate data from another:

```
Agent requests:
  Agent.query("infra", "get_vpc_config", workspace="prod")

Agent responds:
  VpcConfig { cidr: "10.0.0.0/16", subnets: [...] }
```

This is used sparingly — agents default to async pub/sub.

## Model Tier Assignment

All agents are assigned a model tier for cost optimization:

| Tier | Capabilities | Cost | When Used |
|:-----|:------------|:-----|:----------|
| **haiku** | Simple explanations, lookups, documentation formatting | ~5¢ per 1M tokens | Status checks, simple docs, quick explanations |
| **sonnet** | Code implementation, reviews, standard tasks, analysis | ~3x haiku cost | General development, testing, devops tasks |
| **opus** | Architecture decisions, security reviews, complex reasoning | ~3x sonnet cost | Security analysis, system design, complex orchestration |

Tier override is available per-task based on complexity signals detected by the orchestrator.

### Complexity Signals

The orchestrator assesses task complexity:

- **SIMPLE** (haiku tier):
  - Read-only file lookups
  - Formatting and documentation
  - Status checks
  - Explanation of existing code

- **MODERATE** (sonnet tier):
  - Single-file code changes
  - Single-domain task (dev, devops, test, etc.)
  - Implementation within domain expertise
  - Limited cross-service impact

- **COMPLEX** (opus tier):
  - Multi-file architectural changes
  - Cross-domain coordination required
  - Security or compliance implications
  - High blast radius or rollback complexity
  - Requires reasoning about system-wide impacts

## Agent Definition Format

Agents are defined as markdown files in `src/cap/data/agents/` (installed to `~/.claude/agents/` by `cap init`).

### Structure

```markdown
---
name: my-agent
model: sonnet
domain: development
description: One-line purpose
---

# Role

You are a specialist in [domain]. Your responsibility is to [primary task].

## Instructions

### Constraints
- Never [dangerous behavior]
- Always [essential practice]

### Tool Restrictions
- Allowed: Read, Edit, Write, Bash
- Denied: kubectl, terraform apply, git push --force

### Output Contract

Every response MUST include:
1. Analysis section summarizing findings
2. Implementation with concrete code/configs
3. Verification showing testing/validation
4. Cross-links to related docs

## Context

### Before Executing
- Check if related work exists in SharedState
- Subscribe to domain-specific topics on AgentBus

### During Execution
- Log significant decisions to agentdb
- Publish findings to topic: domain/findings
- Stream cost updates to budget tracking

### After Execution
- Publish final output to AgentBus
- Validate output contract before publishing
- Release resources and record metrics
```

### Agent Definition Fields

| Field | Required | Type | Description |
|:------|:---------|:-----|:------------|
| `name` | yes | string | Unique identifier, lowercase with hyphens |
| `model` | yes | enum | Model tier: `haiku`, `sonnet`, or `opus` |
| `domain` | yes | string | Domain category: `dev`, `infra`, `security`, `data`, etc. |
| `description` | yes | string | One-line description of agent's purpose |

The markdown body (after `---` separator) becomes the agent's system prompt. It is passed to the LLM on every invocation.

## Custom Agents

Project teams can create custom agents in `.claude/agents/` (project-level) or `~/.claude/agents/` (user-level).

**Precedence**: Project-level agents override global agents with the same name.

Example custom agent:

```markdown
---
name: my-platform-agent
model: sonnet
domain: custom
description: Platform-specific deployment specialist
---

# Role

You are a specialist in deploying services to our internal Kubernetes platform.

## Instructions

- Always validate Helm chart before deployment
- Check service dependencies in our internal registry
- Use the internal ArgoCD instance at argocd.internal
```

Custom agents are auto-discovered by the orchestrator during initialization.

## Agent Routing Logic

The orchestrator routes tasks using a 3-tier complexity router:

```
Task Input with keywords + file scope
    |
    ├─ Complexity Analysis:
    │  ├─ File count: 1 → INLINE, 2-5 → LIGHTWEIGHT, >5 → FULL
    │  ├─ Keywords: domain mapping (security → security, k8s → devops)
    │  ├─ Cross-repo: no → LIGHTWEIGHT, yes → FULL
    │  └─ Security implications: no → LIGHTWEIGHT, yes → FULL
    |
    v
    ┌─────────────────┐
    │  COMPLEXITY     │
    │  TIER ASSIGNED  │
    └────┬────┬────┬─┘
         │    │    │
    ┌────v────v────v────┐
    │ INLINE LIGHTWEIGHT │ FULL
    │ (haiku) (sonnet) (orchestra)
    └────┬────┬────┬────┘
         │    │    │
    ┌────v────────────────┐
    │ Agent Dispatch      │
    │ (via orchestrator)  │
    └─────────────────────┘
```

### Routing Factors

1. **Task keywords** — semantic matching to agent domains (e.g., "IAM policy" → `iam-policy-review`)
2. **File scope** — number of files and cross-repo dependencies
3. **Complexity signals** — security implications, blast radius, rollback risk
4. **Budget constraints** — prefer cheaper tiers when task permits
5. **Past performance** — recorded routing decisions for continuous improvement

## Cross-References

- [Configuration](configuration.md) — Agent model tier overrides, tool restrictions per workspace
- [Architecture](architecture.md) — System design, agent spawning pipeline, cost tracking
- [CLI Reference](cli-reference.md) — `cap health`, `cap orch-status`, agent diagnostics

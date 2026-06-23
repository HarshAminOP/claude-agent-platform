export const meta = {
  name: 'new-service-deployment',
  description: 'Full multi-agent workflow for deploying a new AWS service (architecture -> infra -> security -> CI/CD -> monitoring -> tests -> docs)',
  whenToUse: 'When setting up a new AWS service, Lambda, EKS workload, or platform component end-to-end',
  phases: [
    { title: 'Architecture', detail: 'Design architecture and service selection', model: 'opus' },
    { title: 'Implementation', detail: 'Terraform, K8s manifests, and pipeline setup' },
    { title: 'Review', detail: 'Security and code quality review', model: 'opus' },
    { title: 'Observability', detail: 'Monitoring, alerts, and runbook' },
    { title: 'Documentation', detail: 'Final docs and summary' }
  ]
}

const SERVICE_SPEC_SCHEMA = {
  type: 'object',
  properties: {
    serviceName: { type: 'string' },
    architecture: { type: 'string' },
    awsServices: { type: 'array', items: { type: 'string' } },
    tradeoffs: { type: 'string' },
    costEstimate: { type: 'string' },
    implementationSteps: { type: 'array', items: { type: 'string' } },
    rollbackPlan: { type: 'string' }
  },
  required: ['serviceName', 'architecture', 'awsServices', 'implementationSteps']
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    passed: { type: 'boolean' },
    findings: { type: 'array', items: { type: 'object', properties: { severity: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' } } } },
    summary: { type: 'string' }
  },
  required: ['passed', 'findings', 'summary']
}

phase('Architecture')
const archDesign = await agent(
  `You are an AWS Solutions Architect for a platform engineering team managing 41+ repos across EKS, ArgoCD, Terraform, and multi-account AWS infrastructure.

Design the architecture for: ${args.description || args}

Context:
- Infrastructure is managed via Terraform + CDK in repos like aws-infra, k8s-infra, o11y-infra
- EKS clusters managed via Terraform + ArgoCD
- Multi-account with AWS Organizations and SCPs
- Observability: Prometheus, Mimir, Grafana, CloudWatch
- All infra follows SSH-only, approval-locked governance

Provide:
1. Clear architecture recommendation with justification
2. AWS services needed and why
3. Trade-offs and alternatives considered
4. Cost direction (increase/decrease/neutral)
5. Ordered implementation steps
6. Rollback plan`,
  { label: 'aws-architect', phase: 'Architecture', schema: SERVICE_SPEC_SCHEMA, model: 'opus', agentType: 'aws-architect' }
)

log(`Architecture designed: ${archDesign.serviceName} using ${archDesign.awsServices.join(', ')}`)

phase('Implementation')
const [terraform, pipeline] = await parallel([
  () => agent(
    `You are a DevOps engineer. Implement the following architecture in Terraform/K8s:

Architecture: ${archDesign.architecture}
AWS Services: ${archDesign.awsServices.join(', ')}
Implementation Steps: ${archDesign.implementationSteps.join('\n')}

Context:
- Terraform repos: aws-infra, k8s-infra, o11y-infra
- EKS managed via Terraform + ArgoCD
- ArgoCD repos: argocd-platform, argocd-appsec
- Use existing patterns from the workspace

Write production-ready Terraform/YAML. Include:
1. Resource definitions
2. Variables and outputs
3. Dependencies
4. Local validation commands`,
    { label: 'devops-terraform', phase: 'Implementation', model: 'sonnet', agentType: 'devops' }
  ),
  () => agent(
    `You are a CI/CD engineer. Design the deployment pipeline for:

Service: ${archDesign.serviceName}
Architecture: ${archDesign.architecture}

Context:
- ArgoCD for GitOps delivery
- GitHub Actions for CI
- ArgoCD ApplicationSets for multi-env

Provide:
1. GitHub Actions workflow
2. ArgoCD Application/ApplicationSet manifest
3. Release gates and rollback strategy
4. Deployment order across environments`,
    { label: 'cicd-pipeline', phase: 'Implementation', model: 'sonnet', agentType: 'cicd' }
  )
])

phase('Review')
const [secReview, codeReview] = await parallel([
  () => agent(
    `You are a Security Engineer. Review this infrastructure for security issues:

Architecture: ${archDesign.architecture}
Terraform output: ${terraform}

Check for:
1. IAM least-privilege violations
2. Open security groups or public resources
3. Missing encryption (at-rest, in-transit)
4. Secrets management issues
5. Network exposure
6. Compliance concerns (SOC2, ISO27001)

Rate each finding: Critical/High/Medium/Low with specific fix.`,
    { label: 'security-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'opus', agentType: 'security' }
  ),
  () => agent(
    `You are a Code Review engineer. Review this infrastructure code for quality:

Terraform: ${terraform}
Pipeline: ${pipeline}

Check for:
1. HCL/YAML best practices
2. Module reuse opportunities
3. Naming conventions
4. Documentation gaps
5. Testing coverage

Rate each finding with severity and specific fix.`,
    { label: 'code-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'code-review' }
  )
])

log(`Security: ${secReview.passed ? 'PASSED' : 'ISSUES FOUND'} (${secReview.findings.length} findings)`)
log(`Code Review: ${codeReview.passed ? 'PASSED' : 'ISSUES FOUND'} (${codeReview.findings.length} findings)`)

let finalTerraform = terraform
let finalPipeline = pipeline
let finalSecReview = secReview
let finalCodeReview = codeReview

if (!secReview.passed || !codeReview.passed) {
  log('Review found issues — starting rework cycle')

  const reworkPrompt = `You are a DevOps engineer. Your previous implementation was reviewed and issues were found. Fix them.

Previous Terraform: ${terraform}
Previous Pipeline: ${pipeline}

Security findings to fix: ${JSON.stringify(secReview.findings)}
Code review findings to fix: ${JSON.stringify(codeReview.findings)}

Fix ALL findings and return the corrected implementation. Do not introduce new issues.`

  finalTerraform = await agent(reworkPrompt, { label: 'devops-rework', phase: 'Review', model: 'sonnet', agentType: 'devops' })

  const [reSecReview, reCodeReview] = await parallel([
    () => agent(
      `Re-review this reworked infrastructure. Previous findings: ${JSON.stringify(secReview.findings)}
Reworked output: ${finalTerraform}
Confirm findings are fixed. Flag any new issues.`,
      { label: 'security-re-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'opus', agentType: 'security' }
    ),
    () => agent(
      `Re-review this reworked code. Previous findings: ${JSON.stringify(codeReview.findings)}
Reworked output: ${finalTerraform}
Confirm findings are fixed. Flag any new issues.`,
      { label: 'code-re-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'code-review' }
    )
  ])

  finalSecReview = reSecReview || secReview
  finalCodeReview = reCodeReview || codeReview
  log(`Re-review: Security ${finalSecReview.passed ? 'PASSED' : 'still has issues'}, Code ${finalCodeReview.passed ? 'PASSED' : 'still has issues'}`)
}

phase('Observability')
const monitoring = await agent(
  `You are an SRE. Design observability for:

Service: ${archDesign.serviceName}
Architecture: ${archDesign.architecture}

Context:
- Prometheus + Mimir for metrics
- Grafana for dashboards
- CloudWatch for AWS-native metrics
- Alerting repos: alerting, alert2jira, monitoring-watcher
- Dashboard repo: grafana-dashboards

Provide:
1. SLO/SLI definitions
2. Prometheus alerting rules (PromQL)
3. Grafana dashboard spec (panels, queries, thresholds)
4. Runbook steps for when alerts fire
5. Validation commands to test alerts`,
  { label: 'sre-monitoring', phase: 'Observability', model: 'sonnet', agentType: 'sre' }
)

phase('Documentation')
const docs = await agent(
  `You are a Documentation engineer. Write the docs for this new service:

Service: ${archDesign.serviceName}
Architecture: ${archDesign.architecture}
Monitoring: ${monitoring}
Security findings: ${secReview.summary}

Write:
1. Service README (purpose, architecture, ownership)
2. Runbook (operational procedures, alert responses)
3. ADR (architecture decision record for why this design)`,
  { label: 'docs', phase: 'Documentation', model: 'haiku', agentType: 'docs' }
)

return {
  service: archDesign.serviceName,
  architecture: archDesign,
  securityReview: finalSecReview,
  codeReview: finalCodeReview,
  implementation: finalTerraform,
  pipeline: finalPipeline,
  monitoring,
  docs,
  allReviewsPassed: finalSecReview.passed && finalCodeReview.passed
}

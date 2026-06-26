export const meta = {
  name: 'cross-repo-impact',
  description: 'Assess cross-repository dependency and deployment impact for a proposed change',
  whenToUse: 'When a change might affect multiple repos, need to understand deployment order, or assess blast radius',
  phases: [
    { title: 'Discover', detail: 'Find all affected repos and components' },
    { title: 'Assess', detail: 'Evaluate impact per repo' },
    { title: 'Plan', detail: 'Deployment order and rollback strategy' }
  ]
}

const IMPACT_SCHEMA = {
  type: 'object',
  properties: {
    repo: { type: 'string' },
    impactLevel: { type: 'string', enum: ['direct', 'transitive', 'potential', 'none'] },
    affectedComponents: { type: 'array', items: { type: 'string' } },
    breakingChange: { type: 'boolean' },
    requiredChanges: { type: 'string' },
    deploymentDependency: { type: 'string' }
  },
  required: ['repo', 'impactLevel', 'breakingChange']
}

phase('Discover')
const change = args.change || args.description || args

const discovery = await agent(
  `You are a platform engineer analyzing cross-repo dependencies in a multi-repo workspace.

Proposed change: ${change}

Workspace repos (grouped by team):
- Delivery-GitOps: argocd-platform, argocd-appsec, argocd-data-applications-platform, argocd-team-config, argocd-grpc-api-gateway, argocd-project-template
- Platform-Core: aws-infra, k8s-infra, okta-infra, dns-infra, o11y-infra, github-infra, jfrog-infra, gcp-infra
- Observability-Alerting: alerting, alert2jira, monitoring-watcher, email-bounce-rate-monitor, eta-monitoring, vehicle-component-monitoring, observability
- Runtime-Services: k8s-audit-label-operator, k8s-wildcard-cert-backup, dap-k8s-sandbox
- Experimental: grafana-dashboards, prometheus-exporters, aws-ce-exporter

Search the workspace to identify:
1. Which repos are DIRECTLY affected by this change
2. Which repos have TRANSITIVE dependencies on affected components
3. Which repos MIGHT be affected (potential impact)
4. Key files/configs that create the dependency

List each affected repo with its relationship to the change.`,
  { label: 'discover-dependencies', phase: 'Discover', model: 'sonnet', agentType: 'devops' }
)

phase('Assess')
const repos = (args.repos || ['aws-infra', 'k8s-infra', 'argocd-platform', 'alerting', 'o11y-infra']).slice(0, 6)

const impacts = await pipeline(
  repos,
  (repo) => agent(
    `You are a platform engineer. Assess the impact of this change on a specific repo:

Change: ${change}
Repo: ${repo}
Discovery context: ${discovery}

Search the repo at repos/**/${repo}/ and assess:
1. Impact level (direct/transitive/potential/none)
2. Which specific components/files are affected
3. Is this a breaking change for this repo?
4. What changes would be needed in this repo?
5. Deployment dependency (must deploy before/after/independent)`,
    { label: `assess-${repo}`, phase: 'Assess', schema: IMPACT_SCHEMA, model: 'sonnet', agentType: 'devops' }
  )
)

phase('Plan')
const validImpacts = impacts.filter(Boolean).filter(i => i.impactLevel !== 'none')

const deployPlan = await agent(
  `You are a CI/CD and DevOps engineer. Create a deployment plan for this cross-repo change:

Change: ${change}
Impacted repos: ${JSON.stringify(validImpacts)}

Create:
1. Deployment ORDER (which repo deploys first, second, etc.)
2. Dependencies between deployments (what blocks what)
3. Validation between steps (what to check before proceeding)
4. Rollback plan for each step
5. Risk assessment (what could go wrong at each step)
6. Communication plan (who needs to know)

Consider ArgoCD sync waves and GitOps reconciliation timing.`,
  { label: 'deployment-plan', phase: 'Plan', model: 'sonnet', agentType: 'cicd' }
)

return {
  change,
  discovery,
  impacts: validImpacts,
  deploymentPlan: deployPlan
}

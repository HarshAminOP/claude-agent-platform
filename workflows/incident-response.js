export const meta = {
  name: 'incident-response',
  description: 'Triage an incident: identify scope, correlate with deployments, propose mitigation, and plan fix',
  whenToUse: 'When triaging an active incident, service degradation, or alert storm',
  phases: [
    { title: 'Triage', detail: 'Identify symptom, scope, and blast radius' },
    { title: 'Correlate', detail: 'Check deployments, config changes, and dependencies' },
    { title: 'Mitigate', detail: 'Propose immediate mitigation and rollback' },
    { title: 'Fix', detail: 'Implement the fix' },
    { title: 'Postmortem', detail: 'Document findings and preventive actions' }
  ]
}

const TRIAGE_SCHEMA = {
  type: 'object',
  properties: {
    symptom: { type: 'string' },
    affectedServices: { type: 'array', items: { type: 'string' } },
    blastRadius: { type: 'string', enum: ['single-service', 'team', 'platform-wide', 'customer-facing'] },
    severity: { type: 'string', enum: ['P1', 'P2', 'P3', 'P4'] },
    hypotheses: { type: 'array', items: { type: 'object', properties: { hypothesis: { type: 'string' }, evidence: { type: 'string' }, likelihood: { type: 'string' } } } },
    immediateActions: { type: 'array', items: { type: 'string' } }
  },
  required: ['symptom', 'affectedServices', 'blastRadius', 'severity', 'hypotheses', 'immediateActions']
}

phase('Triage')
const triage = await agent(
  `You are an SRE triaging an incident for a platform engineering team (EKS, ArgoCD, AWS multi-account).

Incident report: ${args.description || args}

Context:
- Observability: Prometheus, Mimir, Grafana, CloudWatch
- Alerting repos: alerting, alert2jira, monitoring-watcher
- ArgoCD manages deployments via GitOps
- Multi-account AWS with Organizations and SCPs

Perform triage:
1. Identify the symptom and scope
2. Determine blast radius (single-service, team, platform-wide, customer-facing)
3. Assign severity (P1-P4)
4. Generate hypotheses with evidence and likelihood
5. List immediate actions to take`,
  { label: 'sre-triage', phase: 'Triage', schema: TRIAGE_SCHEMA, model: 'sonnet', agentType: 'sre' }
)

log(`Triage: ${triage.severity} - ${triage.blastRadius} - ${triage.affectedServices.join(', ')}`)

phase('Correlate')
const [deployCorrelation, configCorrelation] = await parallel([
  () => agent(
    `You are a CI/CD engineer. Check recent deployments that could correlate with this incident:

Affected services: ${triage.affectedServices.join(', ')}
Symptom: ${triage.symptom}
Hypotheses: ${triage.hypotheses.map(h => h.hypothesis).join('; ')}

Check:
1. Recent ArgoCD syncs and their status
2. Recent git commits to relevant repos
3. Any failed deployments or rollbacks
4. Config changes in the last 24h

Report what changed and when, relative to incident start.`,
    { label: 'deploy-correlation', phase: 'Correlate', model: 'sonnet', agentType: 'cicd' }
  ),
  () => agent(
    `You are a DevOps engineer. Check infrastructure and config changes that could correlate:

Affected services: ${triage.affectedServices.join(', ')}
Symptom: ${triage.symptom}

Check:
1. Recent Terraform changes to aws-infra, k8s-infra, o11y-infra
2. Kubernetes resource changes (HPA, limits, network policies)
3. AWS service quotas or limits
4. DNS or certificate changes
5. External dependency changes

Report findings with timestamps.`,
    { label: 'config-correlation', phase: 'Correlate', model: 'sonnet', agentType: 'devops' }
  )
])

phase('Mitigate')
const mitigation = await agent(
  `You are an SRE. Based on triage and correlation, propose mitigation:

Triage: ${JSON.stringify(triage)}
Deploy correlation: ${deployCorrelation}
Config correlation: ${configCorrelation}

Propose:
1. Immediate mitigation (rollback, scale, circuit-break, redirect)
2. Which specific deployment/change to revert
3. Rollback commands (terraform plan, ArgoCD revert, kubectl)
4. Validation steps to confirm mitigation worked
5. Communication template for stakeholders`,
  { label: 'mitigation-plan', phase: 'Mitigate', model: 'sonnet', agentType: 'sre' }
)

phase('Fix')
const fix = await agent(
  `You are a Dev/DevOps engineer. Implement the fix:

Incident: ${triage.symptom}
Root cause hypothesis: ${triage.hypotheses[0].hypothesis}
Mitigation plan: ${mitigation}

Implement:
1. The actual code/config fix (not just the rollback)
2. A regression test to prevent recurrence
3. Validation steps
4. Safe deployment plan for the fix`,
  { label: 'implement-fix', phase: 'Fix', model: 'sonnet', agentType: 'devops' }
)

phase('Postmortem')
const postmortem = await agent(
  `You are a Docs engineer. Write the incident postmortem:

Incident: ${triage.symptom}
Severity: ${triage.severity}
Affected: ${triage.affectedServices.join(', ')}
Blast radius: ${triage.blastRadius}
Correlation: ${deployCorrelation}
Mitigation: ${mitigation}
Fix: ${fix}

Write a postmortem following this structure:
1. Summary (1 paragraph)
2. Timeline (with timestamps where available)
3. Root cause
4. Impact (duration, affected users/services)
5. Mitigation steps taken
6. Fix applied
7. Action items (preventive measures, monitoring improvements, process changes)
8. Lessons learned`,
  { label: 'postmortem', phase: 'Postmortem', model: 'haiku', agentType: 'docs' }
)

return {
  triage,
  mitigation,
  fix,
  postmortem
}

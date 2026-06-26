export const meta = {
  name: 'security-hardening',
  description: 'Audit and harden infrastructure security: IAM, network, secrets, compliance',
  whenToUse: 'When performing security audits, hardening sprints, or responding to vulnerability findings',
  phases: [
    { title: 'Audit', detail: 'Security scan across IAM, network, secrets', model: 'opus' },
    { title: 'Architecture', detail: 'Design security improvements', model: 'opus' },
    { title: 'Implement', detail: 'Apply fixes to Terraform/K8s' },
    { title: 'Validate', detail: 'Verify fixes and add security gates' }
  ]
}

const FINDING_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          severity: { type: 'string', enum: ['Critical', 'High', 'Medium', 'Low'] },
          category: { type: 'string' },
          description: { type: 'string' },
          affectedResource: { type: 'string' },
          recommendation: { type: 'string' },
          references: { type: 'array', items: { type: 'string' } }
        },
        required: ['id', 'severity', 'category', 'description', 'recommendation']
      }
    },
    summary: { type: 'string' },
    criticalCount: { type: 'number' },
    highCount: { type: 'number' }
  },
  required: ['findings', 'summary', 'criticalCount', 'highCount']
}

phase('Audit')
const target = args.scope || args.description || args || 'full platform'

const auditResults = await parallel([
  () => agent(
    `You are a Security Engineer. Audit IAM policies and access patterns for: ${target}

Context:
- okta-infra repo manages identity (groups, apps, AWS role mappings)
- aws-infra has Organizations with SCPs
- External Secrets Operator pulls from AWS Secrets Manager

Audit:
1. IAM policies for least-privilege violations
2. Permission boundaries and SCPs
3. Cross-account access patterns
4. Service-linked roles
5. Unused or overly broad policies

Report findings with severity, affected resource, and specific fix.`,
    { label: 'audit-iam', phase: 'Audit', schema: FINDING_SCHEMA, model: 'opus', agentType: 'security' }
  ),
  () => agent(
    `You are a Security Engineer. Audit network security for: ${target}

Context:
- EKS clusters with network policies
- VPCs, security groups, NACLs
- PrivateLink and service endpoints
- DNS via dns-infra repo

Audit:
1. Security groups for overly permissive rules
2. Public-facing resources that shouldn't be
3. Network policies in K8s
4. VPC peering and Transit Gateway security
5. Missing encryption in transit

Report findings with severity and specific fix.`,
    { label: 'audit-network', phase: 'Audit', schema: FINDING_SCHEMA, model: 'opus', agentType: 'security' }
  ),
  () => agent(
    `You are a Security Engineer. Audit secrets management for: ${target}

Context:
- External Secrets Operator pulling from AWS Secrets Manager
- ArgoCD manages app deployments
- SSH-only clone policy enforced

Audit:
1. Secrets rotation policies
2. Hardcoded secrets in config/code
3. Secret access patterns and logging
4. Certificate management and expiry
5. Container image vulnerabilities

Report findings with severity and specific fix.`,
    { label: 'audit-secrets', phase: 'Audit', schema: FINDING_SCHEMA, model: 'opus', agentType: 'security' }
  )
])

const allFindings = auditResults.filter(Boolean).flatMap(r => r.findings)
const criticals = allFindings.filter(f => f.severity === 'Critical')
const highs = allFindings.filter(f => f.severity === 'High')

log(`Audit complete: ${criticals.length} critical, ${highs.length} high, ${allFindings.length} total findings`)

phase('Architecture')
const archChanges = await agent(
  `You are an AWS Solutions Architect. Design security architecture improvements based on these findings:

Critical findings: ${JSON.stringify(criticals)}
High findings: ${JSON.stringify(highs)}

Design:
1. Architecture changes needed to address critical/high findings
2. Prioritized implementation order
3. Impact assessment for each change
4. Rollback plan if changes cause issues`,
  { label: 'security-architecture', phase: 'Architecture', model: 'opus', agentType: 'aws-architect' }
)

phase('Implement')
const fixes = await pipeline(
  criticals.concat(highs).slice(0, 8),
  (finding) => agent(
    `You are a DevOps engineer. Implement this security fix:

Finding: ${finding.description}
Severity: ${finding.severity}
Affected: ${finding.affectedResource}
Recommendation: ${finding.recommendation}
Architecture guidance: ${archChanges}

Context:
- Terraform in aws-infra, k8s-infra, okta-infra
- K8s manifests in argocd-platform

Produce the exact Terraform/YAML/config change needed. Include validation command.`,
    { label: `fix-${finding.id}`, phase: 'Implement', model: 'sonnet', agentType: 'devops' }
  )
)

phase('Validate')
const validation = await agent(
  `You are a Security Engineer. Validate the implemented fixes:

Original findings: ${JSON.stringify(criticals.concat(highs).slice(0, 8))}
Fixes applied: ${fixes.filter(Boolean).join('\n---\n')}

For each fix:
1. Does it address the finding completely?
2. Does it introduce new risks?
3. What CI security gates should be added?
4. What ongoing monitoring is needed?

Also recommend pipeline security gates (pre-commit hooks, CI checks, policy-as-code).`,
  { label: 'validate-fixes', phase: 'Validate', model: 'opus', agentType: 'security' }
)

return {
  totalFindings: allFindings.length,
  criticals: criticals.length,
  highs: highs.length,
  findings: allFindings,
  architectureChanges: archChanges,
  fixes: fixes.filter(Boolean),
  validation
}

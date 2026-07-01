export const meta = {
  name: 'infra',
  description: '4-phase infrastructure change with mandatory security review and cost check',
  whenToUse: 'When making infrastructure changes that require architecture design, implementation, security sign-off, and cost awareness',
  phases: [
    { title: 'Design', detail: 'Architecture design for infra change', model: 'opus' },
    { title: 'Implement', detail: 'Terraform/Helm/K8s implementation' },
    { title: 'Review', detail: 'Parallel security + cost review' },
    { title: 'Finalize', detail: 'Address findings, validate' }
  ]
}

const task = args.task || args.description || args || 'infrastructure change'
const workspace = args.workspace || '.'

phase('Design')
const design = await agent(
  `You are an AWS Solutions Architect. Design the infrastructure change for:

Before starting, search the knowledge base for relevant existing patterns using mcp__cap-knowledge__knowledge_search

Task: ${task}
Workspace: ${workspace}

Produce:
1. Architecture diagram (text/ASCII) showing components and data flows
2. List of AWS resources to create/modify/delete
3. Terraform module structure and file layout
4. IAM roles and policies required (least-privilege)
5. Network topology changes (VPC, subnets, security groups)
6. Dependencies and rollback plan`,
  { label: 'design', phase: 'Design', model: 'opus', agentType: 'aws-architect' }
)

phase('Implement')
const implementation = await agent(
  `You are a DevOps engineer. Implement the infrastructure change as production-ready code.

Task: ${task}
Architecture design: ${design}
Workspace: ${workspace}

Produce:
1. Terraform/Helm/K8s files with exact content (no placeholders)
2. Variable definitions with sensible defaults
3. Output values for downstream consumers
4. terraform validate and plan commands to verify
5. Any ArgoCD/Helm values changes required`,
  { label: 'implement', phase: 'Implement', model: 'sonnet', agentType: 'devops' }
)

phase('Review')
const [securityReview, costReview] = await parallel([
  () => agent(
    `You are a Security Engineer. Perform a MANDATORY security review for this infrastructure change.

Task: ${task}
Design: ${design}
Implementation: ${implementation}

Review for:
1. IAM over-permissioning or privilege escalation paths
2. Public exposure of resources that should be private
3. Missing encryption (at rest and in transit)
4. Security group rules too broad (0.0.0.0/0)
5. Missing audit logging or CloudTrail coverage
6. Secrets or credentials hardcoded in config

Return a JSON object with:
- verdict: "APPROVE" or "VETO"
- findings: array of { severity, description, affectedResource, requiredFix }
- summary: string
VETO if any Critical or High severity finding exists that is not already mitigated.`,
    { label: 'security-review', phase: 'Review', model: 'opus', agentType: 'security' }
  ),
  () => agent(
    `You are a Cost Optimization engineer. Review this infrastructure change for cost impact.

Task: ${task}
Implementation: ${implementation}

Analyze:
1. Estimated monthly cost delta (new resources added/removed)
2. Over-provisioned resources (instance sizes, storage, throughput)
3. Missing cost controls (lifecycle policies, auto-scaling, spot usage)
4. Reserved Instance or Savings Plan opportunities
5. Data transfer cost risks

Return advisory findings — these do not block the workflow.`,
    { label: 'cost-review', phase: 'Review', model: 'sonnet', agentType: 'optimization' }
  )
])

// Security VETO loop — max 2 rounds
let finalImplementation = implementation
let securityResult = securityReview
let vetoRounds = 0

while (securityResult?.verdict === 'VETO' && vetoRounds < 2) {
  vetoRounds++
  log(`Security VETO (round ${vetoRounds}/2) — reworking implementation to address findings`)

  finalImplementation = await agent(
    `You are a DevOps engineer. Security has VETOED the implementation. You MUST fix all security findings before proceeding.

Original implementation: ${finalImplementation}
Security findings requiring fixes: ${JSON.stringify(securityResult?.findings || [])}

Fix every finding. Do not skip any. Produce the corrected implementation files.`,
    { label: `security-rework-${vetoRounds}`, phase: 'Finalize', model: 'sonnet', agentType: 'devops' }
  )

  securityResult = await agent(
    `You are a Security Engineer. Re-review the reworked implementation after your VETO.

Reworked implementation: ${finalImplementation}
Original findings: ${JSON.stringify(securityResult?.findings || [])}

Confirm each finding is resolved. Return verdict (APPROVE or VETO) and updated findings.`,
    { label: `security-re-review-${vetoRounds}`, phase: 'Finalize', model: 'opus', agentType: 'security' }
  )
}

if (securityResult?.verdict === 'VETO') {
  log('Security VETO unresolved after 2 rounds — escalating to user')
  // Record outcome for learning
  log("[" + meta.name + "] Recording outcome for learning...")
  await agent(
    'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
    JSON.stringify({ workflow: meta.name, success: false, phases_completed: 3, workspace: args.workspace || '' }),
    { label: 'record-outcome', phase: 'Finalize' }
  )
  return {
    status: 'BLOCKED',
    reason: 'Security VETO not resolved after 2 rework rounds',
    design,
    implementation: finalImplementation,
    securityFindings: securityResult?.findings,
    costFindings: costReview
  }
}

phase('Finalize')
const validation = await agent(
  `You are a DevOps engineer. Finalize the infrastructure change and produce a deployment checklist.

Task: ${task}
Final implementation: ${finalImplementation}
Security approval: ${JSON.stringify(securityResult)}
Cost advisory findings: ${costReview}

Produce:
1. Final file list with paths and a summary of changes
2. Pre-apply checklist (terraform plan review, backup state, notify stakeholders)
3. Apply commands in correct order
4. Post-apply validation steps (smoke tests, health checks)
5. Rollback procedure if apply fails
6. Cost advisory summary (advisory only — does not block)`,
  { label: 'finalize', phase: 'Finalize', model: 'sonnet', agentType: 'devops' }
)

// Record outcome for learning
log("[" + meta.name + "] Recording outcome for learning...")
await agent(
  'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
  JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
  { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
)

return {
  status: 'APPROVED',
  design,
  implementation: finalImplementation,
  securityVerdict: securityResult?.verdict,
  securityFindings: securityResult?.findings,
  costFindings: costReview,
  validation,
  vetoRoundsRequired: vetoRounds
}

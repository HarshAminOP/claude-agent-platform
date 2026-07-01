export const meta = {
  name: 'feature-request',
  description: '5-phase feature implementation with architecture, development, testing, and parallel security review',
  whenToUse: 'When implementing a new feature end-to-end with design, code, tests, and security review',
  phases: [
    { title: 'Plan', detail: 'Architecture and design', model: 'opus' },
    { title: 'Implement', detail: 'Code implementation' },
    { title: 'Test', detail: 'Write and run tests' },
    { title: 'Review', detail: 'Parallel security + code review' },
    { title: 'Finalize', detail: 'Address review findings' }
  ]
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['APPROVE', 'REQUEST_CHANGES', 'VETO'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
          file: { type: 'string' },
          issue: { type: 'string' },
          fix: { type: 'string' }
        }
      }
    }
  },
  required: ['verdict', 'findings']
}

const task = args.task
const workspace = args.workspace || '.'

phase('Plan')
const plan = await agent(
  `You are a Solutions Architect. Design the implementation plan for this feature request:

Before starting, search the knowledge base for relevant existing patterns using mcp__cap-knowledge__knowledge_search

Feature: ${task}
Workspace: ${workspace}

Produce:
1. High-level architecture and component design
2. Files to create or modify (with paths)
3. Interfaces, data structures, and API contracts
4. Edge cases and failure modes to handle
5. Acceptance criteria`,
  { label: 'architect', phase: 'Plan', model: 'opus', agentType: 'aws-architect' }
)

if (!plan) {
  log('Architecture phase failed — aborting')
  await agent(
    'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
    JSON.stringify({ workflow: meta.name, success: false, phases_completed: 0, workspace: args.workspace || '' }),
    { label: 'record-outcome', phase: 'Plan' }
  )
  return { error: 'Plan phase produced no output' }
}

log(`Plan complete: ${plan.slice(0, 120)}...`)

phase('Implement')
const implementation = await agent(
  `You are a Senior Software Engineer. Implement the following feature:

Feature: ${task}
Workspace: ${workspace}

Architecture plan:
${plan}

Requirements:
- Follow existing code patterns in the workspace
- Include proper error handling
- Add inline comments for non-obvious logic
- Do not introduce security vulnerabilities (OWASP top 10)`,
  { label: 'dev', phase: 'Implement', model: 'sonnet', agentType: 'dev' }
)

if (!implementation) {
  log('Implementation phase failed — aborting')
  await agent(
    'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
    JSON.stringify({ workflow: meta.name, success: false, phases_completed: 1, workspace: args.workspace || '' }),
    { label: 'record-outcome', phase: 'Implement' }
  )
  return { error: 'Implement phase produced no output', plan }
}

log('Implementation complete')

phase('Test')
const tests = await agent(
  `You are a QA Engineer. Write and run tests for this implementation:

Feature: ${task}
Workspace: ${workspace}

Implementation:
${implementation}

Requirements:
1. Unit tests for all new functions/methods
2. Integration tests for external boundaries
3. Edge case and error path coverage
4. Run the tests and report results`,
  { label: 'test', phase: 'Test', model: 'sonnet', agentType: 'test' }
)

log(`Tests ${tests ? 'complete' : 'failed or skipped'}`)

phase('Review')
const reviews = await parallel([
  () => agent(
    `You are a Security Engineer. Review this implementation for security issues:

Feature: ${task}
Workspace: ${workspace}

Implementation:
${implementation}

Check for: injection flaws, broken auth, sensitive data exposure, insecure dependencies, misconfiguration, OWASP top 10.
Return structured verdict and findings.`,
    { label: 'security-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'security' }
  ),
  () => agent(
    `You are a Senior Engineer doing a code review:

Feature: ${task}
Workspace: ${workspace}

Implementation:
${implementation}

Review for: correctness bugs, missing error handling, logic errors, off-by-one, null handling, accidental debug code.
Return structured verdict and findings.`,
    { label: 'code-review', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'code-review' }
  )
])

const allFindings = reviews.filter(Boolean).flatMap(r => r.findings || [])
const blocking = allFindings.filter(f => f.severity === 'CRITICAL' || f.severity === 'HIGH')
const verdicts = reviews.filter(Boolean).map(r => r.verdict)

log(`Review complete: ${blocking.length} blocking findings, verdicts: ${verdicts.join(', ')}`)

phase('Finalize')
let finalOutput = implementation

if (blocking.length > 0) {
  log(`Fixing ${blocking.length} HIGH/CRITICAL findings`)
  finalOutput = await agent(
    `You are a Senior Software Engineer. Fix the following HIGH/CRITICAL review findings:

Feature: ${task}
Workspace: ${workspace}

Original implementation:
${implementation}

Findings to fix:
${JSON.stringify(blocking, null, 2)}

Apply all fixes and return the complete updated implementation.`,
    { label: 'finalize', phase: 'Finalize', model: 'sonnet', agentType: 'dev' }
  )
} else {
  log('No blocking findings — implementation approved as-is')
}

// Record outcome for learning
log("[" + meta.name + "] Recording outcome for learning...")
await agent(
  'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
  JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
  { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
)

return {
  plan,
  implementation: finalOutput || implementation,
  tests,
  findings: allFindings,
  blockingCount: blocking.length,
  verdicts
}

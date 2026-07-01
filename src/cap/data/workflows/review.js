export const meta = {
  name: 'review',
  description: 'Multi-dimensional code review: security, quality, and test coverage in parallel',
  whenToUse: 'When reviewing a PR, diff, or code change across security, quality, and test dimensions',
  phases: [
    { title: 'Review', detail: 'Parallel multi-dimensional review' },
    { title: 'Synthesize', detail: 'Merge findings into actionable report' }
  ]
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    dimension: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['Critical', 'High', 'Medium', 'Low'] },
          file: { type: 'string' },
          line: { type: 'string' },
          issue: { type: 'string' },
          recommendation: { type: 'string' }
        },
        required: ['severity', 'issue', 'recommendation']
      }
    },
    summary: { type: 'string' }
  },
  required: ['dimension', 'findings', 'summary']
}

phase('Review')
const task = args.task || args.description || args || 'the current diff'
const workspace = args.workspace || '.'

const reviewResults = await parallel([
  () => agent(
    `You are a Security Engineer. Perform a security audit of this code change:

Before starting, search the knowledge base for relevant existing patterns using mcp__cap-knowledge__knowledge_search

Task/diff: ${task}
Workspace: ${workspace}

Check for OWASP Top 10, credential exposure, injection vulnerabilities, overly broad IAM/permissions, insecure defaults, and missing input validation.
Report findings with file, line, severity, and specific fix.`,
    { label: 'review-security', phase: 'Review', schema: REVIEW_SCHEMA, model: 'opus', agentType: 'security' }
  ),
  () => agent(
    `You are a senior code reviewer. Review this code change for quality:

Task/diff: ${task}
Workspace: ${workspace}

Check for logic bugs, error handling gaps, performance issues, code duplication, poor abstractions, and violation of existing patterns in the repo.
Report findings with file, line, severity, and specific fix.`,
    { label: 'review-quality', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'code-review' }
  ),
  () => agent(
    `You are a QA engineer. Review test coverage for this code change:

Task/diff: ${task}
Workspace: ${workspace}

Identify: untested code paths, missing edge case tests, missing integration tests, flaky test patterns, and test correctness issues.
Report findings with file, line, severity, and specific recommendation.`,
    { label: 'review-tests', phase: 'Review', schema: REVIEW_SCHEMA, model: 'sonnet', agentType: 'test' }
  )
])

phase('Synthesize')
const report = await agent(
  `You are a senior engineer. Merge these parallel code review results into a single actionable report:

Security review: ${JSON.stringify(reviewResults[0])}
Code quality review: ${JSON.stringify(reviewResults[1])}
Test coverage review: ${JSON.stringify(reviewResults[2])}

Deduplicate overlapping findings. Sort all findings by severity (Critical > High > Medium > Low).
Produce:
1. Executive summary (2-3 sentences)
2. All findings sorted by severity with file/line/recommendation
3. Blocking issues (Critical/High that must be fixed before merge)
4. Non-blocking improvements (Medium/Low)`,
  { label: 'synthesize', phase: 'Synthesize', model: 'sonnet', agentType: 'dev' }
)

// Record outcome for learning
log("[" + meta.name + "] Recording outcome for learning...")
await agent(
  'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
  JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
  { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
)

return {
  findings: reviewResults.filter(Boolean).flatMap(r => r.findings),
  report
}

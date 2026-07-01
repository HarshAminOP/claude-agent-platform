export const meta = {
  name: 'bugfix',
  description: '3-phase bug fix with research, implementation with regression test, and review',
  whenToUse: 'When triaging and fixing a known bug end-to-end',
  phases: [
    { title: 'Research', detail: 'Reproduce and find root cause' },
    { title: 'Fix', detail: 'Implement fix with regression test' },
    { title: 'Review', detail: 'Code review of the fix' }
  ]
}

const task = args.task || args.description || args
const workspace = args.workspace || '.'

phase('Research')
const research = await agent(
  `You are a senior developer debugging a bug.
Before starting, search the knowledge base for relevant existing patterns using mcp__cap-knowledge__knowledge_search
Bug: ${task}
Workspace: ${workspace}
1. Reproduce — identify the exact input/condition that triggers the bug
2. Trace execution to find the root cause
3. List every file involved (with line numbers)
4. State the root cause in one sentence
5. Propose the minimal fix (no refactoring beyond bug scope)`,
  { label: 'bug-research', phase: 'Research', model: 'sonnet', agentType: 'dev' }
)

log('Research complete — root cause identified')

phase('Fix')
const fix = await agent(
  `You are a senior developer. Implement the bug fix.
Bug: ${task}
Workspace: ${workspace}
Research: ${research}
1. Apply the minimal fix from research
2. Write a regression test that would have caught this bug
3. Confirm no unrelated code was changed
4. Summarize files modified and what changed`,
  { label: 'bug-fix', phase: 'Fix', model: 'sonnet', agentType: 'dev' }
)

log('Fix implemented')

phase('Review')
const review = await agent(
  `You are a code reviewer. Review this bug fix.
Bug: ${task}
Fix: ${fix}
Check: correctness, regression test coverage, side effects, security.
Respond with APPROVED or REQUEST_CHANGES listing each finding as CRITICAL/HIGH/MEDIUM/LOW.`,
  { label: 'fix-review', phase: 'Review', model: 'sonnet', agentType: 'code-review' }
)

const reviewText = JSON.stringify(review)
const needsRework = /REQUEST_CHANGES/.test(reviewText) && /(CRITICAL|HIGH)/.test(reviewText)

if (needsRework) {
  log('Review found CRITICAL/HIGH issues — applying one rework round')
  const rework = await agent(
    `You are a senior developer. Address the review findings.
Original fix: ${fix}
Review findings: ${review}
Fix only the CRITICAL and HIGH issues. Do not change anything else.`,
    { label: 'bug-rework', phase: 'Review', model: 'sonnet', agentType: 'dev' }
  )
  // Record outcome for learning
  log("[" + meta.name + "] Recording outcome for learning...")
  await agent(
    'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
    JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
    { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
  )
  return { research, fix: rework, review, reworkApplied: true }
}

// Record outcome for learning
log("[" + meta.name + "] Recording outcome for learning...")
await agent(
  'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
  JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
  { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
)

return { research, fix, review, reworkApplied: false }

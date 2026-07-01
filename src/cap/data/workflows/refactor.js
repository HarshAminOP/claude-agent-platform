export const meta = {
  name: 'refactor',
  description: '4-phase refactoring with analysis, implementation, regression verification, and review',
  whenToUse: 'When refactoring code with safety guarantees: analysis, implementation, test verification, and code review',
  phases: [
    { title: 'Analyze', detail: 'Understand current code and plan refactor approach' },
    { title: 'Implement', detail: 'Execute the refactoring' },
    { title: 'Verify', detail: 'Run tests to ensure no regression' },
    { title: 'Review', detail: 'Code review of refactored code' }
  ]
}

const task = args.task || args.description || args
const workspace = args.workspace || '.'

phase('Analyze')
const analysis = await agent(
  `Senior engineer. Analyze code for refactoring.
Before starting, search the knowledge base for relevant existing patterns using mcp__cap-knowledge__knowledge_search
Task: ${task} | Workspace: ${workspace}
Read relevant source files, identify issues, plan changes with file paths. Note existing tests.`,
  { label: 'analyze-code', phase: 'Analyze', model: 'sonnet', agentType: 'dev' }
)
log('Analysis complete — proceeding to implementation')

phase('Implement')
const implementation = await agent(
  `Senior engineer. Execute the refactoring plan.
Task: ${task} | Workspace: ${workspace}
Plan: ${analysis}
Apply changes. Preserve behaviour — only restructure/rename/reorganise. List every modified file.`,
  { label: 'implement-refactor', phase: 'Implement', model: 'sonnet', agentType: 'dev' }
)

phase('Verify')
let verification = await agent(
  `Test engineer. Run existing test suite after refactoring.
Workspace: ${workspace} | Changes: ${implementation}
Run all tests. Report pass/fail counts. Quote exact errors for failures. State if failures are pre-existing or new.`,
  { label: 'verify-tests', phase: 'Verify', model: 'sonnet', agentType: 'test' }
)

if (/fail|error/i.test(String(verification))) {
  log('Test failures detected — attempting regression fix')
  const fix = await agent(
    `Senior engineer. Fix regressions introduced by refactoring.
Workspace: ${workspace} | Refactoring: ${implementation} | Failures: ${verification}
Fix only regressions caused by this refactoring. Do not touch unrelated code or tests.`,
    { label: 'fix-regression', phase: 'Verify', model: 'sonnet', agentType: 'dev' }
  )

  verification = await agent(
    `Test engineer. Re-run tests after regression fix.
Workspace: ${workspace} | Fix: ${fix}
Report pass/fail counts. State clearly whether the refactoring is now safe to merge.`,
    { label: 'verify-rerun', phase: 'Verify', model: 'sonnet', agentType: 'test' }
  )

  if (/fail|error/i.test(String(verification))) {
    log('Tests still failing after fix attempt — escalating to user')
    // Record outcome for learning
    log("[" + meta.name + "] Recording outcome for learning...")
    await agent(
      'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
      JSON.stringify({ workflow: meta.name, success: false, phases_completed: 2, workspace: args.workspace || '' }),
      { label: 'record-outcome', phase: 'Verify' }
    )
    return { analysis, implementation, verification, status: 'TESTS_FAILING' }
  }
}

phase('Review')
const review = await agent(
  `Code reviewer. Review refactoring quality.
Task: ${task} | Workspace: ${workspace}
Changes: ${implementation} | Tests: ${verification}
Check: behaviour preserved, readability improved, conventions followed, edge cases handled.
Verdict: APPROVE or REQUEST_CHANGES with specific comments.`,
  { label: 'review-refactor', phase: 'Review', model: 'sonnet', agentType: 'code-review' }
)

// Record outcome for learning
log("[" + meta.name + "] Recording outcome for learning...")
await agent(
  'Record this workflow outcome. Call mcp__cap-session__session_record with event_type=workflow_complete and content=' +
  JSON.stringify({ workflow: meta.name, success: true, phases_completed: meta.phases.length, workspace: args.workspace || '' }),
  { label: 'record-outcome', phase: meta.phases[meta.phases.length - 1].title }
)

return { analysis, implementation, verification, review, status: 'COMPLETE' }

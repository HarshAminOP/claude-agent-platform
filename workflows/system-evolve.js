export const meta = {
  name: 'system-evolve',
  description: 'Audit and optimize the entire Claude Code agent system — agents, workflows, permissions, knowledge base',
  whenToUse: 'When the user asks to improve, fix, or evolve the agent system itself',
  phases: [
    { title: 'Audit', detail: 'Scan all system components for issues' },
    { title: 'Diagnose', detail: 'Identify specific problems and improvements' },
    { title: 'Fix', detail: 'Apply targeted improvements' },
    { title: 'Verify', detail: 'Confirm system coherence after changes' },
  ],
}

phase('Audit')

const auditResults = await parallel([
  () => agent(
    `Read and audit ~/.claude/agents/ — check every .md file has valid YAML frontmatter (name, description, model fields). Check that prompts are clear, actionable, and don't overlap with other agents. Report any issues found.`,
    { label: 'audit:agents', phase: 'Audit', agentType: 'system', schema: {
      type: 'object',
      properties: {
        agents: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, issues: { type: 'array', items: { type: 'string' } }, suggestions: { type: 'array', items: { type: 'string' } } } } },
        totalAgents: { type: 'number' },
        criticalIssues: { type: 'number' }
      }
    }}
  ),
  () => agent(
    `Read and audit ~/.claude/workflows/ — check every .js file has valid meta block, uses agentType on all agent() calls, and follows the correct workflow API (phase, agent, parallel, pipeline). Report any issues found.`,
    { label: 'audit:workflows', phase: 'Audit', agentType: 'system', schema: {
      type: 'object',
      properties: {
        workflows: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, issues: { type: 'array', items: { type: 'string' } }, suggestions: { type: 'array', items: { type: 'string' } } } } },
        totalWorkflows: { type: 'number' },
        criticalIssues: { type: 'number' }
      }
    }}
  ),
  () => agent(
    `Read and audit ~/.claude/settings.json — check permissions are comprehensive for agent autonomy (Read, Write, Edit, git operations, common tools all allowed). Check deny rules are safe. Check model and env config. Report any issues or gaps.`,
    { label: 'audit:permissions', phase: 'Audit', agentType: 'system', schema: {
      type: 'object',
      properties: {
        allowCount: { type: 'number' },
        denyCount: { type: 'number' },
        gaps: { type: 'array', items: { type: 'string' } },
        issues: { type: 'array', items: { type: 'string' } },
        suggestions: { type: 'array', items: { type: 'string' } }
      }
    }}
  ),
  () => agent(
    `Read and audit ~/.claude/knowledge/ — check INDEX.md is complete, domain files cover the key concepts, repo summaries exist for all repos. Report gaps or stale content.`,
    { label: 'audit:knowledge', phase: 'Audit', agentType: 'system', schema: {
      type: 'object',
      properties: {
        indexComplete: { type: 'boolean' },
        domainFiles: { type: 'number' },
        repoFiles: { type: 'number' },
        taskFiles: { type: 'number' },
        gaps: { type: 'array', items: { type: 'string' } },
        staleEntries: { type: 'array', items: { type: 'string' } }
      }
    }}
  ),
])

phase('Diagnose')

const diagnosis = await agent(
  `You have the results of a system audit across 4 dimensions:

AGENTS: ${JSON.stringify(auditResults[0])}
WORKFLOWS: ${JSON.stringify(auditResults[1])}
PERMISSIONS: ${JSON.stringify(auditResults[2])}
KNOWLEDGE: ${JSON.stringify(auditResults[3])}

Synthesize these findings into a prioritized action plan. Group by:
1. CRITICAL — things that are broken and block functionality
2. HIGH — things that degrade quality or cause friction
3. MEDIUM — improvements that would make things better
4. LOW — nice-to-haves

For each item, specify exactly what file to change and what the change should be.`,
  { label: 'diagnose:synthesize', phase: 'Diagnose', agentType: 'system', schema: {
    type: 'object',
    properties: {
      critical: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' } } } },
      high: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' } } } },
      medium: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' } } } },
      low: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' } } } },
      summary: { type: 'string' }
    }
  }}
)

log(`Diagnosis complete: ${diagnosis.critical.length} critical, ${diagnosis.high.length} high, ${diagnosis.medium.length} medium, ${diagnosis.low.length} low`)

phase('Fix')

const fixes = [...diagnosis.critical, ...diagnosis.high]
if (fixes.length > 0) {
  await pipeline(
    fixes,
    (fix) => agent(
      `Apply this fix to the Claude Code agent system:

FILE: ${fix.file}
ISSUE: ${fix.description}
FIX: ${fix.fix}

Read the file, make the minimal change needed, and verify the file is still valid after the change. For agent .md files ensure YAML frontmatter is preserved. For .js workflow files ensure the meta block and API calls are correct. For settings.json ensure valid JSON.`,
      { label: `fix:${fix.file.split('/').pop()}`, phase: 'Fix', agentType: 'system' }
    )
  )
}

phase('Verify')

const verification = await agent(
  `Verify the Claude Code agent system is coherent after fixes were applied:

1. Read ~/.claude/CLAUDE.md — check agent roster matches actual agents in ~/.claude/agents/
2. Spot-check 3 agent files for valid YAML frontmatter
3. Spot-check 2 workflow files for valid meta blocks and agentType usage
4. Read ~/.claude/settings.json — confirm it's valid JSON with correct structure
5. Check ~/.claude/knowledge/INDEX.md exists and is readable

Report the system health status.`,
  { label: 'verify:coherence', phase: 'Verify', agentType: 'system', schema: {
    type: 'object',
    properties: {
      healthy: { type: 'boolean' },
      claudeMdConsistent: { type: 'boolean' },
      agentsValid: { type: 'boolean' },
      workflowsValid: { type: 'boolean' },
      settingsValid: { type: 'boolean' },
      knowledgeAccessible: { type: 'boolean' },
      issues: { type: 'array', items: { type: 'string' } }
    }
  }}
)

return {
  diagnosis: diagnosis.summary,
  fixesApplied: fixes.length,
  mediumDeferred: diagnosis.medium.length,
  lowDeferred: diagnosis.low.length,
  systemHealthy: verification.healthy,
  verificationDetails: verification
}

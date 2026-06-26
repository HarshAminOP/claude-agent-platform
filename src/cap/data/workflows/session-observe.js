export const meta = {
  name: 'session-observe',
  description: 'Analyze session interactions and adapt the system — save learnings to memory, update preferences, evolve agent behavior',
  whenToUse: 'Run at end of substantial sessions, after user corrections, or when the user says /session-observe',
  phases: [
    { title: 'Analyze', detail: 'Review session observations and extract patterns' },
    { title: 'Adapt', detail: 'Update memory, preferences, and system config' },
  ],
}

const observations = args || 'No specific observations provided — analyze what can be improved based on available memory and system state.'

phase('Analyze')

const analysis = await agent(
  `You are the session observer for a platform engineering AI system. Your job is to analyze interactions and extract actionable learnings.

OBSERVATIONS FROM THIS SESSION:
${typeof observations === 'string' ? observations : JSON.stringify(observations)}

Your task:
1. Read the current project memory files (check ~/.claude/projects/ for the current workspace memory dir) to understand what's already captured
2. Read ~/.claude/CLAUDE.md to understand current behavior rules
3. Based on the observations, identify:
   - NEW user preferences not yet captured (communication style, tool preferences, workflow habits)
   - CORRECTIONS to existing behavior (things that should change)
   - PATTERNS worth codifying (recurring task types, preferred approaches)
   - KNOWLEDGE gaps (things the system should know but doesn't)
   - AGENT improvements (prompts that could be sharper, routing that could be better)

Be specific. Don't create generic "be better" observations. Only surface things that would change how the system behaves in a future session.`,
  { label: 'analyze:session', phase: 'Analyze', agentType: 'system', schema: {
    type: 'object',
    properties: {
      newPreferences: { type: 'array', items: { type: 'object', properties: {
        category: { type: 'string' },
        observation: { type: 'string' },
        actionable: { type: 'string' }
      }}},
      corrections: { type: 'array', items: { type: 'object', properties: {
        current: { type: 'string' },
        shouldBe: { type: 'string' },
        file: { type: 'string' }
      }}},
      patterns: { type: 'array', items: { type: 'object', properties: {
        pattern: { type: 'string' },
        frequency: { type: 'string' },
        automation: { type: 'string' }
      }}},
      knowledgeGaps: { type: 'array', items: { type: 'string' } },
      agentImprovements: { type: 'array', items: { type: 'object', properties: {
        agent: { type: 'string' },
        improvement: { type: 'string' }
      }}},
      summary: { type: 'string' }
    }
  }}
)

log(`Analysis: ${analysis.newPreferences.length} preferences, ${analysis.corrections.length} corrections, ${analysis.patterns.length} patterns`)

phase('Adapt')

const adaptations = []

if (analysis.newPreferences.length > 0 || analysis.corrections.length > 0) {
  adaptations.push(agent(
    `Update the memory system based on these session learnings:

NEW PREFERENCES TO SAVE:
${JSON.stringify(analysis.newPreferences, null, 2)}

CORRECTIONS TO APPLY:
${JSON.stringify(analysis.corrections, null, 2)}

Instructions:
- For new preferences: create or update memory files in the project's memory directory
  - Use type "feedback" for behavior corrections
  - Use type "user" for preference/style observations
  - Update MEMORY.md index if you create new files
- For corrections: edit the specified file to fix the behavior
- Use kebab-case filenames: feedback_<topic>.md or user_<topic>.md
- Include frontmatter: name, description, metadata.type
- Body: lead with the rule, then **Why:** and **How to apply:**
- Do NOT duplicate existing memories — update them if they overlap`,
    { label: 'adapt:memory', phase: 'Adapt', agentType: 'system' }
  ))
}

if (analysis.agentImprovements.length > 0) {
  adaptations.push(agent(
    `Apply these targeted improvements to agent prompts:

${JSON.stringify(analysis.agentImprovements, null, 2)}

Instructions:
- Read the agent file at ~/.claude/agents/<agent>.md
- Make the minimal edit needed to address the improvement
- Preserve YAML frontmatter (name, description, model)
- Don't restructure the whole file — just add/modify the relevant section
- If the improvement is vague or would make things worse, skip it`,
    { label: 'adapt:agents', phase: 'Adapt', agentType: 'system' }
  ))
}

if (analysis.knowledgeGaps.length > 0) {
  adaptations.push(agent(
    `Address these knowledge gaps in the centralized knowledge base at ~/.claude/knowledge/:

GAPS:
${JSON.stringify(analysis.knowledgeGaps, null, 2)}

Instructions:
- For domain concepts: create/update files in ~/.claude/knowledge/domains/
- For repo-specific knowledge: update files in ~/.claude/knowledge/repos/
- For task outcomes: create files in ~/.claude/knowledge/tasks/
- Update ~/.claude/knowledge/INDEX.md if you add new files
- Only write what you can VERIFY from files in the workspace — don't hallucinate
- If a gap requires reading repos you can't access right now, skip it and note that`,
    { label: 'adapt:knowledge', phase: 'Adapt', agentType: 'system' }
  ))
}

if (adaptations.length > 0) {
  await parallel(adaptations.map(a => () => a))
}

return {
  summary: analysis.summary,
  preferencesCapured: analysis.newPreferences.length,
  correctionsApplied: analysis.corrections.length,
  patternsIdentified: analysis.patterns.length,
  knowledgeGapsFilled: analysis.knowledgeGaps.length,
  agentImprovements: analysis.agentImprovements.length
}

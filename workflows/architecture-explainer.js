export const meta = {
  name: 'architecture-explainer',
  description: 'Deep architecture walkthrough with dependency mapping, risk analysis, and learning path',
  whenToUse: 'When asking "how does X work", "explain the architecture of Y", or onboarding to a platform domain',
  phases: [
    { title: 'Map', detail: 'Discover architecture and dependencies' },
    { title: 'Explain', detail: 'Multi-perspective explanation' },
    { title: 'Synthesize', detail: 'Combined walkthrough with learning path' }
  ]
}

phase('Map')
const topic = args.topic || args.description || args

const [structure, dependencies, operations] = await parallel([
  () => agent(
    `You are a platform architect. Map the structure of: ${topic}

Search the workspace (repos/) to find all relevant repos, files, and configs.

Produce:
1. Which repos are involved
2. Key files and their roles
3. How components are organized (modules, charts, stacks)
4. Entry points and interfaces
5. Configuration hierarchy`,
    { label: 'map-structure', phase: 'Map', model: 'sonnet', agentType: 'aws-architect' }
  ),
  () => agent(
    `You are a platform architect. Map the dependencies for: ${topic}

Search the workspace to identify:
1. Upstream dependencies (what does this consume/depend on)
2. Downstream dependents (what depends on this)
3. External service dependencies (AWS, third-party)
4. Shared infrastructure (VPCs, IAM roles, secrets)
5. Deployment dependencies (what must exist first)`,
    { label: 'map-dependencies', phase: 'Map', model: 'sonnet', agentType: 'aws-architect' }
  ),
  () => agent(
    `You are an SRE. Map the operational aspects of: ${topic}

Search the workspace for:
1. How is this deployed? (ArgoCD apps, Terraform, Helm)
2. How is this monitored? (alerts, dashboards, SLOs)
3. What are the failure modes?
4. What does the runbook say?
5. Recent incidents or changes (from git history)`,
    { label: 'map-operations', phase: 'Map', model: 'sonnet', agentType: 'sre' }
  )
])

phase('Explain')
const [systemView, tradeoffs] = await parallel([
  () => agent(
    `You are a Teacher/Architect. Explain the system-level view of: ${topic}

Based on this research:
Structure: ${structure}
Dependencies: ${dependencies}
Operations: ${operations}

Explain:
1. The big picture - what problem does this solve and for whom
2. How data/requests flow through the system
3. Key architectural decisions and WHY they were made
4. How this fits into the broader platform
5. What would break if this disappeared`,
    { label: 'system-explanation', phase: 'Explain', model: 'sonnet', agentType: 'teacher' }
  ),
  () => agent(
    `You are a senior architect. Analyze the design trade-offs of: ${topic}

Based on this research:
Structure: ${structure}
Dependencies: ${dependencies}

Analyze:
1. What trade-offs were made (consistency vs availability, cost vs performance, etc.)
2. What alternatives exist and why they weren't chosen
3. Current limitations and technical debt
4. Evolution path (where is this heading)
5. Risks and single points of failure`,
    { label: 'tradeoff-analysis', phase: 'Explain', model: 'opus', agentType: 'aws-architect' }
  )
])

phase('Synthesize')
const synthesis = await agent(
  `You are a Teacher. Create a comprehensive learning walkthrough for: ${topic}

Combine these perspectives:
System view: ${systemView}
Trade-offs: ${tradeoffs}
Structure: ${structure}
Operations: ${operations}

Produce a learning-optimized walkthrough:
1. One-paragraph summary (the "elevator pitch")
2. System-level diagram description (boxes and arrows)
3. Component-by-component walkthrough (ordered for learning, not alphabetically)
4. Key files to read (ordered from most important to deep-dive)
5. Hands-on exploration tasks (3-5 concrete things to try)
6. Common misconceptions
7. Prerequisites and related topics to explore next`,
  { label: 'learning-synthesis', phase: 'Synthesize', model: 'haiku', agentType: 'teacher' }
)

return {
  topic,
  structure,
  dependencies,
  operations,
  systemView,
  tradeoffs,
  learningWalkthrough: synthesis
}

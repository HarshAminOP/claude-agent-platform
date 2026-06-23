export const meta = {
  name: 'repo-health-check',
  description: 'Multi-dimensional health check across workspace repos: security, reliability, code quality, docs',
  whenToUse: 'When auditing workspace health, preparing for reviews, or checking repo quality across dimensions',
  phases: [
    { title: 'Scan', detail: 'Parallel scan across dimensions' },
    { title: 'Verify', detail: 'Adversarially verify findings' },
    { title: 'Report', detail: 'Synthesize health report' }
  ]
}

const HEALTH_SCHEMA = {
  type: 'object',
  properties: {
    dimension: { type: 'string' },
    score: { type: 'number', minimum: 0, maximum: 10 },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          repo: { type: 'string' },
          issue: { type: 'string' },
          severity: { type: 'string' },
          recommendation: { type: 'string' }
        }
      }
    },
    topRisks: { type: 'array', items: { type: 'string' } }
  },
  required: ['dimension', 'score', 'findings', 'topRisks']
}

const DIMENSIONS = [
  {
    key: 'security',
    prompt: `Scan workspace repos for security health:
1. Are there secrets, tokens, or credentials in code/config?
2. Do repos have security scanning (Dependabot, Wiz, etc.)?
3. Are IAM policies least-privilege?
4. Are container images pinned to digests?
5. Are there public-facing resources that shouldn't be?`
  },
  {
    key: 'reliability',
    prompt: `Scan workspace repos for reliability health:
1. Do services have SLOs defined?
2. Are there meaningful alerts (not just default CloudWatch)?
3. Do services have runbooks?
4. Are there single points of failure?
5. Is there auto-scaling and circuit breaking?`
  },
  {
    key: 'code-quality',
    prompt: `Scan workspace repos for code quality health:
1. Is there test coverage?
2. Are there linting/formatting configs?
3. Is the code well-structured (modules, separation of concerns)?
4. Are there code review requirements?
5. Is there dead code or unused dependencies?`
  },
  {
    key: 'documentation',
    prompt: `Scan workspace repos for documentation health:
1. Do repos have meaningful READMEs?
2. Are there ADRs for key decisions?
3. Are there runbooks for operational procedures?
4. Is there API documentation?
5. Are diagrams current?`
  }
]

phase('Scan')
const target = args.repos || args.scope || 'all workspace repos'

const scanResults = await pipeline(
  DIMENSIONS,
  (d) => agent(
    `You are a platform quality engineer. ${d.prompt}

Target: ${target}
Search the workspace repos/ directory. Check actual files, not assumptions.

Score 0-10 (10=excellent). List specific findings per repo with severity.`,
    { label: `scan-${d.key}`, phase: 'Scan', schema: HEALTH_SCHEMA, model: 'sonnet', agentType: d.key === 'security' ? 'security' : d.key === 'reliability' ? 'sre' : d.key === 'code-quality' ? 'code-review' : 'docs' }
  )
)

phase('Verify')
const allFindings = scanResults.filter(Boolean).flatMap(r => r.findings).filter(f => f.severity === 'High' || f.severity === 'Critical')

const verified = await pipeline(
  allFindings.slice(0, 10),
  (finding) => agent(
    `Adversarially verify this health check finding. Try to REFUTE it.

Finding: ${finding.issue}
Repo: ${finding.repo}
Severity: ${finding.severity}

Search the actual repo at repos/**/${finding.repo}/ and check:
1. Is this actually true? (check the files)
2. Is the severity correct?
3. Could there be a valid reason for this?

Return: confirmed=true if the finding holds, confirmed=false if you refuted it, with reasoning.`,
    { label: `verify-${finding.repo}`, phase: 'Verify', model: 'sonnet', agentType: 'code-review' }
  )
)

phase('Report')
const report = await agent(
  `You are a platform engineering lead. Synthesize this health check into an actionable report:

Scan results: ${JSON.stringify(scanResults.filter(Boolean).map(r => ({ dimension: r.dimension, score: r.score, topRisks: r.topRisks })))}
Verified findings: ${verified.filter(Boolean).join('\n')}

Produce:
1. Executive summary (2-3 sentences)
2. Scorecard (dimension -> score)
3. Top 5 action items (highest ROI improvements)
4. Quick wins (things that can be fixed in <1 hour)
5. Strategic improvements (need planning)`,
  { label: 'health-report', phase: 'Report', model: 'haiku', agentType: 'docs' }
)

return {
  scores: scanResults.filter(Boolean).map(r => ({ dimension: r.dimension, score: r.score })),
  findings: allFindings,
  verifiedFindings: verified.filter(Boolean),
  report
}

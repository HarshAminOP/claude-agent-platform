export const meta = {
  name: 'cost-optimization',
  description: 'Identify cost waste, propose optimizations, implement changes, verify no reliability regression',
  whenToUse: 'When running a cost optimization sprint, investigating high AWS bills, or right-sizing resources',
  phases: [
    { title: 'Identify', detail: 'Find cost waste across infrastructure', model: 'opus' },
    { title: 'Alternatives', detail: 'Propose cost-effective architectures', model: 'opus' },
    { title: 'Implement', detail: 'Make the changes' },
    { title: 'Verify', detail: 'Ensure no reliability regression' }
  ]
}

phase('Identify')
const target = args.scope || args.description || args || 'full platform'

const wasteAnalysis = await parallel([
  () => agent(
    `You are a Cost Optimization engineer. Analyze compute costs for: ${target}

Context:
- EKS clusters defined in k8s-infra
- Lambdas in various infra repos
- EC2 instances managed via Terraform

Look for:
1. Over-provisioned EKS node groups (CPU/memory requests vs limits vs actual)
2. Lambda over-allocation (memory, timeout)
3. Unused or idle resources
4. Missing auto-scaling or incorrect thresholds
5. Reserved Instance / Savings Plan opportunities

Quantify waste where possible (% over-provisioned, estimated monthly savings).`,
    { label: 'compute-waste', phase: 'Identify', model: 'opus', agentType: 'optimization' }
  ),
  () => agent(
    `You are a Cost Optimization engineer. Analyze storage and data transfer costs for: ${target}

Context:
- S3 buckets in aws-infra
- EBS volumes attached to EKS nodes
- Data transfer between accounts/regions/services
- Observability data (Prometheus/Mimir storage, CloudWatch logs)

Look for:
1. S3 buckets without lifecycle policies
2. Unattached EBS volumes
3. Cross-AZ/region data transfer that could be eliminated
4. Log retention too long for low-value logs
5. Missing S3 Intelligent Tiering or Glacier transitions

Quantify waste where possible.`,
    { label: 'storage-waste', phase: 'Identify', model: 'opus', agentType: 'optimization' }
  ),
  () => agent(
    `You are a Cost Optimization engineer. Analyze networking and service costs for: ${target}

Context:
- NAT Gateways in VPCs
- Load balancers (ALB/NLB)
- DNS queries (Route53)
- Data transfer through NAT, VPC peering, Transit Gateway

Look for:
1. NAT Gateway optimization (VPC endpoints instead)
2. Unused load balancers
3. Excessive DNS query costs
4. Transit Gateway vs VPC peering cost comparison
5. CloudFront caching opportunities

Quantify waste where possible.`,
    { label: 'network-waste', phase: 'Identify', model: 'opus', agentType: 'optimization' }
  )
])

const allWaste = wasteAnalysis.filter(Boolean)
log(`Cost analysis complete across ${allWaste.length} dimensions`)

phase('Alternatives')
const alternatives = await agent(
  `You are an AWS Solutions Architect. Based on these cost findings, propose alternative architectures:

Findings:
${allWaste.join('\n---\n')}

For each major waste area:
1. Current architecture and its cost profile
2. Proposed alternative with estimated savings
3. Migration complexity (Low/Medium/High)
4. Risk to reliability/performance
5. Implementation priority (quick wins first)

Rank recommendations by ROI (savings vs effort).`,
  { label: 'architecture-alternatives', phase: 'Alternatives', model: 'opus', agentType: 'aws-architect' }
)

phase('Implement')
const implementation = await agent(
  `You are a DevOps engineer. Implement the top cost optimization recommendations:

Recommendations: ${alternatives}

For each quick-win (Low complexity, High savings):
1. Exact Terraform changes needed
2. File paths to modify
3. terraform plan output expectations
4. Rollback plan

Focus on changes that can be safely applied incrementally.`,
  { label: 'implement-savings', phase: 'Implement', model: 'sonnet', agentType: 'devops' }
)

phase('Verify')
const verification = await agent(
  `You are an SRE. Verify that the proposed cost optimizations don't regress reliability:

Changes proposed: ${implementation}

For each change:
1. What SLOs/SLIs could be affected?
2. What monitoring should be watched during rollout?
3. What are the rollback triggers?
4. What load testing should be done first?
5. Recommended rollout schedule (% canary, soak time)`,
  { label: 'reliability-check', phase: 'Verify', model: 'sonnet', agentType: 'sre' }
)

return {
  wasteAnalysis: allWaste,
  alternatives,
  implementation,
  reliabilityVerification: verification
}

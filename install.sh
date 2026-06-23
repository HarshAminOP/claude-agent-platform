#!/bin/bash
set -euo pipefail

# ============================================================================
# Claude Agent Platform — Installer
# A self-improving multi-agent system for Claude Code CLI
#
# Prerequisites: Claude Code CLI installed (https://claude.ai/code)
# ============================================================================

VERSION="1.0.0"
CLAUDE_DIR="$HOME/.claude"
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

header() { echo -e "\n${CYAN}${BOLD}$1${NC}"; }
success() { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
error() { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  $1"; }

# ============================================================================
# Pre-flight checks
# ============================================================================

echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Claude Agent Platform Installer v${VERSION}    ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""

header "Pre-flight checks"

# Check Claude CLI
if command -v claude >/dev/null 2>&1; then
  CLAUDE_VERSION=$(claude --version 2>/dev/null || echo "unknown")
  success "Claude Code CLI found: $CLAUDE_VERSION"
else
  error "Claude Code CLI not found!"
  echo "    Install from: https://claude.ai/code"
  exit 1
fi

# Check required tools
for tool in git node npm python3; do
  if command -v "$tool" >/dev/null 2>&1; then
    success "$tool available"
  else
    warn "$tool not found — some features may not work"
  fi
done

# Check uvx (for Python MCP servers)
if command -v uvx >/dev/null 2>&1; then
  success "uvx available (for AWS MCP servers)"
else
  warn "uvx not found — AWS MCP servers won't be available"
  warn "Install with: pip install uv"
fi

# ============================================================================
# Configuration prompts
# ============================================================================

header "Configuration"

echo ""
echo "Choose your AI provider setup:"
echo "  1) Anthropic API (direct)"
echo "  2) AWS Bedrock"
echo "  3) Skip (configure later)"
echo ""
read -p "  Provider [1/2/3]: " PROVIDER_CHOICE
PROVIDER_CHOICE="${PROVIDER_CHOICE:-3}"

case "$PROVIDER_CHOICE" in
  1)
    PROVIDER="anthropic"
    read -p "  Default model [opus/sonnet/haiku]: " DEFAULT_MODEL
    DEFAULT_MODEL="${DEFAULT_MODEL:-opus}"
    ;;
  2)
    PROVIDER="bedrock"
    read -p "  AWS Region [eu-central-1]: " AWS_REGION
    AWS_REGION="${AWS_REGION:-eu-central-1}"
    read -p "  AWS Profile for Bedrock access: " BEDROCK_PROFILE
    BEDROCK_PROFILE="${BEDROCK_PROFILE:-default}"
    read -p "  Default model [opus/sonnet/haiku]: " DEFAULT_MODEL
    DEFAULT_MODEL="${DEFAULT_MODEL:-opus}"
    ;;
  *)
    PROVIDER="skip"
    DEFAULT_MODEL="opus"
    ;;
esac

echo ""
echo "AWS SSO Configuration (for MCP servers — optional):"
read -p "  SSO session name (leave blank to skip): " SSO_SESSION
if [ -n "$SSO_SESSION" ]; then
  read -p "  Default AWS profile for reads: " AWS_READ_PROFILE
  AWS_READ_PROFILE="${AWS_READ_PROFILE:-default}"
fi

echo ""
echo "Git conventions:"
read -p "  Branch prefix pattern [TICKET/slug]: " BRANCH_PATTERN
BRANCH_PATTERN="${BRANCH_PATTERN:-TICKET/slug}"
read -p "  Use SSH for git (y/n) [y]: " USE_SSH
USE_SSH="${USE_SSH:-y}"

echo ""
read -p "  Effort level [low/medium/high/max]: " EFFORT_LEVEL
EFFORT_LEVEL="${EFFORT_LEVEL:-high}"

# ============================================================================
# Install agents
# ============================================================================

header "Installing agents (14 specialists)"

mkdir -p "$CLAUDE_DIR/agents"

for agent_file in "$PACKAGE_DIR/agents/"*.md; do
  if [ -f "$agent_file" ]; then
    cp "$agent_file" "$CLAUDE_DIR/agents/"
    success "$(basename "$agent_file" .md)"
  fi
done

# ============================================================================
# Install workflows
# ============================================================================

header "Installing workflows (10 pipelines)"

mkdir -p "$CLAUDE_DIR/workflows"

for workflow_file in "$PACKAGE_DIR/workflows/"*.js; do
  if [ -f "$workflow_file" ]; then
    cp "$workflow_file" "$CLAUDE_DIR/workflows/"
    success "$(basename "$workflow_file" .js)"
  fi
done

# ============================================================================
# Install scripts
# ============================================================================

header "Installing scripts"

mkdir -p "$CLAUDE_DIR/scripts"

for script_file in "$PACKAGE_DIR/scripts/"*.sh; do
  if [ -f "$script_file" ]; then
    cp "$script_file" "$CLAUDE_DIR/scripts/"
    chmod +x "$CLAUDE_DIR/scripts/$(basename "$script_file")"
    success "$(basename "$script_file")"
  fi
done

# ============================================================================
# Initialize knowledge base
# ============================================================================

header "Setting up knowledge base"

mkdir -p "$CLAUDE_DIR/knowledge"/{repos,domains,tasks}

if [ ! -f "$CLAUDE_DIR/knowledge/INDEX.md" ]; then
  cp "$PACKAGE_DIR/knowledge/INDEX.md" "$CLAUDE_DIR/knowledge/"
  success "Created knowledge base index"
else
  warn "Knowledge base already exists — preserving"
fi

# ============================================================================
# Generate CLAUDE.md
# ============================================================================

header "Generating global CLAUDE.md"

# Build security section based on git preference
if [ "$USE_SSH" = "y" ] || [ "$USE_SSH" = "Y" ]; then
  SECURITY_GIT="- SSH-only repo URLs"
else
  SECURITY_GIT="- HTTPS repo URLs with credential helper"
fi

# Build AWS section
AWS_SECTION=""
if [ -n "$SSO_SESSION" ]; then
  AWS_SECTION="## AWS Access

### SSO Login
- Run \`~/.claude/scripts/aws-sso-login.sh\` to authenticate
- Run \`~/.claude/scripts/aws-sso-login.sh status\` to check session health
- If a session is expired and an agent needs AWS access, run the login script FIRST

### Profile Management
- MCP servers inherit AWS_PROFILE from env
- Ask which profile before first AWS call in a session if the target is ambiguous
- Default to read-only if not specified
- Always pass \`--profile <name>\` explicitly on CLI calls

### MCP Servers (read-only AWS access)
- \`aws-iam\` — List/get IAM roles, policies, users, groups
- \`aws-eks\` — Describe clusters, nodegroups, addons
- \`aws-cloudwatch\` — Query logs, metrics, alarms
- \`aws-lambda\` — List/describe functions, get config
- \`aws-pricing\` — Get service pricing, compare instance costs
- \`aws-iac\` — Validate CloudFormation/CDK templates
- \`aws-docs\` — Search AWS documentation
- \`terraform\` — Terraform registry lookups
- \`kubernetes\` — K8s resources and pod logs

These servers use the active SSO session. If they fail with auth errors → run SSO login."
fi

# Generate from template
sed \
  -e "s|{{SECURITY_GIT}}|$SECURITY_GIT|g" \
  -e "s|{{BRANCH_PATTERN}}|$BRANCH_PATTERN|g" \
  -e "s|{{AWS_SECTION}}|$AWS_SECTION|g" \
  "$PACKAGE_DIR/templates/CLAUDE.md.tmpl" > "$CLAUDE_DIR/CLAUDE.md"

success "Generated ~/.claude/CLAUDE.md"

# ============================================================================
# Generate settings.json
# ============================================================================

header "Generating settings.json"

# Build env block
ENV_BLOCK='{}'
case "$PROVIDER" in
  bedrock)
    ENV_BLOCK=$(python3 -c "
import json
env = {
    'CLAUDE_CODE_USE_BEDROCK': '1',
    'AWS_REGION': '$AWS_REGION',
    'AWS_PROFILE': '$BEDROCK_PROFILE'
}
print(json.dumps(env))
")
    ;;
  anthropic)
    ENV_BLOCK='{}'
    ;;
  *)
    ENV_BLOCK='{}'
    ;;
esac

# Generate settings from template
python3 -c "
import json

env = $ENV_BLOCK
settings = {
    'env': env,
    'model': '$DEFAULT_MODEL',
    'effortLevel': '$EFFORT_LEVEL',
    'permissions': {
        'allow': [
            'Read(*)',
            'Edit(*)',
            'Write(*)',
            'Bash(grep:*)',
            'Bash(find:*)',
            'Bash(ls:*)',
            'Bash(cat:*)',
            'Bash(head:*)',
            'Bash(tail:*)',
            'Bash(wc:*)',
            'Bash(sort:*)',
            'Bash(uniq:*)',
            'Bash(diff:*)',
            'Bash(git log*)',
            'Bash(git diff*)',
            'Bash(git status*)',
            'Bash(git branch*)',
            'Bash(git show*)',
            'Bash(git blame*)',
            'Bash(git rev-parse*)',
            'Bash(git checkout*)',
            'Bash(git add*)',
            'Bash(git commit*)',
            'Bash(git stash*)',
            'Bash(git worktree*)',
            'Bash(git fetch*)',
            'Bash(git pull*)',
            'Bash(terraform plan*)',
            'Bash(terraform show*)',
            'Bash(terraform fmt*)',
            'Bash(terraform validate*)',
            'Bash(terraform state list*)',
            'Bash(terraform state show*)',
            'Bash(cdk synth*)',
            'Bash(cdk diff*)',
            'Bash(kubectl get*)',
            'Bash(kubectl describe*)',
            'Bash(kubectl diff*)',
            'Bash(kubectl logs*)',
            'Bash(helm template*)',
            'Bash(helm lint*)',
            'Bash(helm show*)',
            'Bash(gh pr list*)',
            'Bash(gh pr view*)',
            'Bash(gh issue list*)',
            'Bash(gh issue view*)',
            'Bash(gh repo view*)',
            'Bash(gh api*)',
            'Bash(npm test*)',
            'Bash(npm run*)',
            'Bash(npm install*)',
            'Bash(go test*)',
            'Bash(go build*)',
            'Bash(go vet*)',
            'Bash(go mod*)',
            'Bash(python -m pytest*)',
            'Bash(pip install*)',
            'Bash(make*)',
            'Bash(jq:*)',
            'Bash(yq:*)',
            'Bash(aws sso login*)',
            'Bash(aws sso list*)',
            'Bash(aws configure*)',
            'Bash(aws sts get-caller-identity*)',
            'Bash(aws s3 ls*)',
            'Bash(aws ec2 describe*)',
            'Bash(aws iam list*)',
            'Bash(aws iam get*)',
            'Bash(aws eks describe*)',
            'Bash(aws eks list*)',
            'Bash(aws logs*)',
            'Bash(aws cloudwatch*)',
            'Bash(mkdir*)',
            'Bash(chmod*)',
            'Bash(cp:*)',
            'Bash(mv:*)',
            'Bash(touch:*)',
            'Bash(basename:*)',
            'Bash(dirname:*)',
            'Bash(realpath:*)',
            'Bash(which:*)',
            'Bash(type:*)',
            'Bash(env:*)',
            'Bash(echo:*)',
            'Bash(printf:*)',
            'Bash(date:*)',
            'Bash(xargs:*)',
            'Bash(sed:*)',
            'Bash(awk:*)',
            'Bash(tr:*)',
            'Bash(cut:*)',
            'Bash(tee:*)',
            'Bash(./scripts/*)',
            'Bash(bash scripts/*)',
            'Bash(bash ./*)',
            'Bash(~/.claude/scripts/*)',
            'Bash(bash ~/.claude/scripts/*)'
        ],
        'deny': [
            'Bash(terraform apply*)',
            'Bash(terraform destroy*)',
            'Bash(cdk deploy*)',
            'Bash(kubectl apply*)',
            'Bash(kubectl delete*)',
            'Bash(kubectl drain*)',
            'Bash(git push --force*)',
            'Bash(git push*)',
            'Bash(rm -rf*)'
        ]
    }
}

# Remove empty env
if not env:
    del settings['env']

with open('$CLAUDE_DIR/settings.json', 'w') as f:
    json.dump(settings, f, indent=2)
    f.write('\n')
"

success "Generated ~/.claude/settings.json"

# ============================================================================
# Install MCP servers
# ============================================================================

header "Installing MCP servers"

# Always install these (no auth needed)
claude mcp add --scope user terraform -- npx -y terraform-mcp-server 2>/dev/null && success "terraform (registry lookups)" || warn "terraform — failed"
claude mcp add --scope user kubernetes -- npx -y mcp-server-kubernetes 2>/dev/null && success "kubernetes (cluster access)" || warn "kubernetes — failed"

# AWS MCP servers (only if uvx available and SSO configured)
if command -v uvx >/dev/null 2>&1 && [ -n "$SSO_SESSION" ]; then
  claude mcp add --scope user aws-docs -- uvx awslabs.aws-documentation-mcp-server@latest 2>/dev/null && success "aws-docs" || warn "aws-docs — failed"
  claude mcp add --scope user aws-iam -- uvx awslabs.iam-mcp-server 2>/dev/null && success "aws-iam (read-only)" || warn "aws-iam — failed"
  claude mcp add --scope user aws-eks -- uvx awslabs.eks-mcp-server 2>/dev/null && success "aws-eks (read-only)" || warn "aws-eks — failed"
  claude mcp add --scope user aws-cloudwatch -- uvx awslabs.cloudwatch-mcp-server 2>/dev/null && success "aws-cloudwatch (read-only)" || warn "aws-cloudwatch — failed"
  claude mcp add --scope user aws-lambda -- uvx awslabs.lambda-tool-mcp-server 2>/dev/null && success "aws-lambda (read-only)" || warn "aws-lambda — failed"
  claude mcp add --scope user aws-pricing -- uvx awslabs.aws-pricing-mcp-server 2>/dev/null && success "aws-pricing" || warn "aws-pricing — failed"
  claude mcp add --scope user aws-iac -- uvx awslabs.aws-iac-mcp-server 2>/dev/null && success "aws-iac (linting)" || warn "aws-iac — failed"
elif [ -z "$SSO_SESSION" ]; then
  info "Skipping AWS MCP servers (no SSO session configured)"
  info "Run installer again with SSO session to add them later"
fi

# ============================================================================
# Configure AWS SSO script
# ============================================================================

if [ -n "$SSO_SESSION" ]; then
  header "Configuring AWS SSO"

  # Patch the SSO login script with the user's session name
  sed -i.bak "s|SSO_SESSION=\".*\"|SSO_SESSION=\"$SSO_SESSION\"|" "$CLAUDE_DIR/scripts/aws-sso-login.sh"
  rm -f "$CLAUDE_DIR/scripts/aws-sso-login.sh.bak"
  success "SSO session configured: $SSO_SESSION"
fi

# ============================================================================
# Summary
# ============================================================================

header "Installation complete!"
echo ""
echo -e "  ${BOLD}What's installed:${NC}"
echo "  ├── 14 specialist agents (orchestrator, devops, security, ...)"
echo "  ├── 10 multi-agent workflows (deploy, incident, audit, ...)"
echo "  ├── 3 utility scripts (auto-sync, init-repo, aws-sso-login)"
echo "  ├── Knowledge base framework"
echo "  ├── Global permissions (read-heavy, write-safe)"
if [ -n "$SSO_SESSION" ]; then
echo "  └── 9 MCP servers (AWS read-only + Terraform + K8s)"
else
echo "  └── 2 MCP servers (Terraform + K8s)"
fi
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo "  1. Open any directory and run 'claude' — agents work everywhere"
echo "  2. Initialize a workspace: ~/.claude/scripts/init-repo.sh ."
if [ -n "$SSO_SESSION" ]; then
echo "  3. Authenticate AWS: ~/.claude/scripts/aws-sso-login.sh"
fi
echo ""
echo -e "  ${BOLD}Quick test:${NC}"
echo "  $ claude"
echo "  > review this codebase for security issues"
echo "  (Security + code-review agents will run in parallel)"
echo ""
echo -e "  ${BOLD}Customize:${NC}"
echo "  $ claude"
echo "  > improve the agent system (routes to the system agent)"
echo "  > add a new agent for database migrations"
echo ""
echo -e "${GREEN}${BOLD}Ready to go!${NC} Run 'claude' in any directory."

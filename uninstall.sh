#!/bin/bash
set -euo pipefail

# Claude Agent Platform — Uninstaller
# Removes agents, workflows, scripts, and settings. Preserves knowledge base.

CLAUDE_DIR="$HOME/.claude"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}Claude Agent Platform — Uninstaller${NC}"
echo ""

# Confirm
read -p "This will remove agents, workflows, scripts, and settings. Knowledge base will be preserved. Continue? (y/N): " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Cancelled."
  exit 0
fi

echo ""

# Remove agents
if [ -d "$CLAUDE_DIR/agents" ]; then
  rm -rf "$CLAUDE_DIR/agents"
  echo -e "${GREEN}✓${NC} Removed agents/"
fi

# Remove workflows
if [ -d "$CLAUDE_DIR/workflows" ]; then
  rm -rf "$CLAUDE_DIR/workflows"
  echo -e "${GREEN}✓${NC} Removed workflows/"
fi

# Remove scripts
if [ -d "$CLAUDE_DIR/scripts" ]; then
  rm -rf "$CLAUDE_DIR/scripts"
  echo -e "${GREEN}✓${NC} Removed scripts/"
fi

# Remove CLAUDE.md
if [ -f "$CLAUDE_DIR/CLAUDE.md" ]; then
  rm "$CLAUDE_DIR/CLAUDE.md"
  echo -e "${GREEN}✓${NC} Removed CLAUDE.md"
fi

# Remove settings (backup first)
if [ -f "$CLAUDE_DIR/settings.json" ]; then
  cp "$CLAUDE_DIR/settings.json" "$CLAUDE_DIR/settings.json.bak"
  rm "$CLAUDE_DIR/settings.json"
  echo -e "${GREEN}✓${NC} Removed settings.json (backup: settings.json.bak)"
fi

# Remove MCP servers
if command -v claude >/dev/null 2>&1; then
  for server in aws-iam aws-eks aws-cloudwatch aws-lambda aws-pricing aws-iac aws-docs terraform kubernetes; do
    claude mcp remove --scope user "$server" 2>/dev/null && echo -e "${GREEN}✓${NC} Removed MCP: $server" || true
  done
fi

echo ""
echo -e "${YELLOW}Preserved:${NC}"
echo "  ~/.claude/knowledge/ (your accumulated knowledge)"
echo "  ~/.claude/settings.json.bak (your previous settings)"
echo ""
echo -e "${GREEN}Uninstall complete.${NC} Run install.sh to reinstall."

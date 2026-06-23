#!/bin/bash
set -euo pipefail

# Claude Agent Platform — Upgrade
# Updates agents, workflows, and scripts while preserving user config

CLAUDE_DIR="$HOME/.claude"
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}Claude Agent Platform — Upgrade${NC}"
echo ""

# Check what's already installed
if [ ! -d "$CLAUDE_DIR/agents" ]; then
  echo "No existing installation found. Run install.sh instead."
  exit 1
fi

# Upgrade agents
echo -e "${CYAN}Upgrading agents...${NC}"
for agent_file in "$PACKAGE_DIR/agents/"*.md; do
  if [ -f "$agent_file" ]; then
    cp "$agent_file" "$CLAUDE_DIR/agents/"
    echo -e "  ${GREEN}✓${NC} $(basename "$agent_file" .md)"
  fi
done

# Upgrade workflows
echo -e "${CYAN}Upgrading workflows...${NC}"
for workflow_file in "$PACKAGE_DIR/workflows/"*.js; do
  if [ -f "$workflow_file" ]; then
    cp "$workflow_file" "$CLAUDE_DIR/workflows/"
    echo -e "  ${GREEN}✓${NC} $(basename "$workflow_file" .js)"
  fi
done

# Upgrade scripts
echo -e "${CYAN}Upgrading scripts...${NC}"
for script_file in "$PACKAGE_DIR/scripts/"*.sh; do
  if [ -f "$script_file" ]; then
    cp "$script_file" "$CLAUDE_DIR/scripts/"
    chmod +x "$CLAUDE_DIR/scripts/$(basename "$script_file")"
    echo -e "  ${GREEN}✓${NC} $(basename "$script_file")"
  fi
done

echo ""
echo -e "${YELLOW}Preserved (not overwritten):${NC}"
echo "  ~/.claude/settings.json (your permissions and env config)"
echo "  ~/.claude/CLAUDE.md (your orchestrator instructions)"
echo "  ~/.claude/knowledge/ (your accumulated knowledge)"
echo "  MCP server configuration"
echo ""
echo -e "${GREEN}Upgrade complete.${NC}"
echo ""
echo "To also update CLAUDE.md and settings.json, run install.sh (it will overwrite them)."

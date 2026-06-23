#!/bin/bash
# Initialize a repo or workspace for Claude Code integration
# Creates project-level settings, indexes in knowledge base
#
# Usage: ~/.claude/scripts/init-repo.sh [repo-path]

REPO_PATH="${1:-.}"
REPO_PATH=$(cd "$REPO_PATH" && pwd)
REPO_NAME=$(basename "$REPO_PATH")
KB_DIR="$HOME/.claude/knowledge/repos"

echo "Initializing Claude Code integration for: $REPO_NAME"
echo "Path: $REPO_PATH"

# 1. Ensure .claude directory exists
mkdir -p "$REPO_PATH/.claude"

# 2. Create project-level settings with sync hook (if git repo)
if [ -d "$REPO_PATH/.git" ]; then
  if [ ! -f "$REPO_PATH/.claude/settings.json" ]; then
    cat > "$REPO_PATH/.claude/settings.json" << 'EOF'
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "SessionStart",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/scripts/auto-sync.sh >/dev/null 2>&1 &"
          }
        ]
      }
    ]
  }
}
EOF
    echo "✓ Created .claude/settings.json with sync hook"
  else
    echo "• .claude/settings.json already exists, skipping"
  fi
fi

# 3. Index the repo in knowledge base
if [ -d "$REPO_PATH/.git" ]; then
  echo "Indexing repo in knowledge base..."
  mkdir -p "$KB_DIR"

  # Detect languages
  LANGUAGES=""
  [ -f "$REPO_PATH/go.mod" ] && LANGUAGES="Go"
  [ -f "$REPO_PATH/package.json" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }TypeScript/JavaScript"
  [ -f "$REPO_PATH/requirements.txt" ] || [ -f "$REPO_PATH/pyproject.toml" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Python"
  [ -f "$REPO_PATH/Cargo.toml" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Rust"
  [ -n "$(find "$REPO_PATH" -maxdepth 2 -name '*.tf' 2>/dev/null | head -1)" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Terraform"
  [ -n "$(find "$REPO_PATH" -maxdepth 2 -name '*.java' 2>/dev/null | head -1)" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Java"
  [ -n "$(find "$REPO_PATH" -maxdepth 2 -name '*.cs' 2>/dev/null | head -1)" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }C#"
  [ -z "$LANGUAGES" ] && LANGUAGES="Unknown"

  # Get description
  DESCRIPTION=$(git -C "$REPO_PATH" log --oneline -1 2>/dev/null || echo "New repository")

  # Get directory structure
  STRUCTURE=$(find "$REPO_PATH" -maxdepth 2 -type d \
    ! -path '*/.git/*' ! -path '*/node_modules/*' ! -path '*/.terraform/*' \
    ! -path '*/__pycache__/*' ! -path '*/vendor/*' ! -path '*/.worktrees/*' \
    ! -path '*/.next/*' ! -path '*/dist/*' ! -path '*/build/*' \
    2>/dev/null | sed "s|$REPO_PATH/||" | sort | head -30)

  # Get recent commits
  COMMITS=$(git -C "$REPO_PATH" log --oneline -5 2>/dev/null || echo "No commits yet")

  # Determine group (parent dir name)
  PARENT_DIR=$(basename "$(dirname "$REPO_PATH")")
  [ "$PARENT_DIR" = "." ] && PARENT_DIR="Standalone"

  # Write knowledge file
  KB_FILE="$KB_DIR/${PARENT_DIR}--${REPO_NAME}.md"
  cat > "$KB_FILE" << EOF
# $REPO_NAME

**Group:** $PARENT_DIR
**Languages:** $LANGUAGES
**Path:** $REPO_PATH

## Description
$DESCRIPTION

## Structure
\`\`\`
$STRUCTURE
\`\`\`

## Recent Commits
\`\`\`
$COMMITS
\`\`\`

## Key Files
$(find "$REPO_PATH" -maxdepth 1 -type f \( -name "*.md" -o -name "Makefile" -o -name "Dockerfile" -o -name "*.yaml" -o -name "*.yml" -o -name "*.toml" -o -name "*.json" \) 2>/dev/null | sed "s|$REPO_PATH/|- |" | head -15)

---
*Auto-indexed: $(date +%Y-%m-%d)*
EOF
  echo "✓ Created knowledge file: $KB_FILE"

  # Update index if it exists
  if [ -f "$KB_DIR/_index.md" ]; then
    if ! grep -q "$REPO_NAME" "$KB_DIR/_index.md" 2>/dev/null; then
      echo "| $REPO_NAME | $PARENT_DIR | $LANGUAGES |" >> "$KB_DIR/_index.md"
      echo "✓ Added to repo index"
    fi
  fi
fi

# 4. Add to .gitignore if not already there
if [ -f "$REPO_PATH/.gitignore" ]; then
  if ! grep -q "\.claude/" "$REPO_PATH/.gitignore" 2>/dev/null; then
    echo "" >> "$REPO_PATH/.gitignore"
    echo "# Claude Code local config" >> "$REPO_PATH/.gitignore"
    echo ".claude/" >> "$REPO_PATH/.gitignore"
    echo "✓ Added .claude/ to .gitignore"
  fi
elif [ -d "$REPO_PATH/.git" ]; then
  echo "# Claude Code local config" > "$REPO_PATH/.gitignore"
  echo ".claude/" >> "$REPO_PATH/.gitignore"
  echo "✓ Created .gitignore with .claude/ exclusion"
fi

echo ""
echo "Done! Claude Code is ready for: $REPO_NAME"
echo "  - All agents, workflows, and knowledge base are global (already active)"
echo "  - Repo sync hook installed"
echo "  - Repo indexed in knowledge base"

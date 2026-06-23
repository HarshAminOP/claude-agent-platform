#!/bin/bash
# Build/rebuild the knowledge base repo index
# Scans all git repos under the current workspace and indexes them
#
# Usage: ~/.claude/scripts/build-knowledge-base.sh [workspace-path]

WORKSPACE="${1:-$(pwd)}"
KB_DIR="$HOME/.claude/knowledge/repos"
MAX_DEPTH=3

echo "Building knowledge base from: $WORKSPACE"
echo "Target: $KB_DIR"
echo ""

mkdir -p "$KB_DIR"

# Find all repos
repos=$(find "$WORKSPACE" -maxdepth $MAX_DEPTH -name ".git" -type d 2>/dev/null | sed 's/\/.git$//' | sort)

if [ -z "$repos" ]; then
  echo "No git repos found under $WORKSPACE (depth $MAX_DEPTH)"
  exit 0
fi

count=$(echo "$repos" | wc -l | tr -d ' ')
echo "Found $count repos"
echo ""

# Start index file
INDEX_FILE="$KB_DIR/_index.md"
cat > "$INDEX_FILE" << EOF
# Repository Index

Auto-generated: $(date +%Y-%m-%d)
Source: $WORKSPACE

| Repo | Group | Languages |
|------|-------|-----------|
EOF

processed=0

for repo in $repos; do
  REPO_NAME=$(basename "$repo")
  PARENT_DIR=$(basename "$(dirname "$repo")")
  [ "$PARENT_DIR" = "." ] && PARENT_DIR="Standalone"

  # Detect languages
  LANGUAGES=""
  [ -f "$repo/go.mod" ] && LANGUAGES="Go"
  [ -f "$repo/package.json" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }TypeScript/JS"
  [ -f "$repo/requirements.txt" ] || [ -f "$repo/pyproject.toml" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Python"
  [ -f "$repo/Cargo.toml" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Rust"
  [ -n "$(find "$repo" -maxdepth 2 -name '*.tf' 2>/dev/null | head -1)" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Terraform"
  [ -n "$(find "$repo" -maxdepth 2 -name '*.java' 2>/dev/null | head -1)" ] && LANGUAGES="${LANGUAGES:+$LANGUAGES, }Java"
  [ -z "$LANGUAGES" ] && LANGUAGES="—"

  # Add to index
  echo "| $REPO_NAME | $PARENT_DIR | $LANGUAGES |" >> "$INDEX_FILE"

  # Get structure
  STRUCTURE=$(find "$repo" -maxdepth 2 -type d \
    ! -path '*/.git/*' ! -path '*/node_modules/*' ! -path '*/.terraform/*' \
    ! -path '*/__pycache__/*' ! -path '*/vendor/*' \
    2>/dev/null | sed "s|$repo/||" | sort | head -25)

  # Get recent commits
  COMMITS=$(git -C "$repo" log --oneline -5 2>/dev/null || echo "No commits")

  # Key files
  KEY_FILES=$(find "$repo" -maxdepth 1 -type f \( -name "*.md" -o -name "Makefile" -o -name "Dockerfile" -o -name "*.yaml" -o -name "*.yml" -o -name "*.toml" -o -name "*.json" \) 2>/dev/null | sed "s|$repo/|- |" | head -10)

  # Terraform resources (if applicable)
  TF_RESOURCES=""
  if [ -n "$(find "$repo" -maxdepth 2 -name '*.tf' 2>/dev/null | head -1)" ]; then
    TF_RESOURCES=$(grep -rh '^resource ' "$repo" --include='*.tf' 2>/dev/null | sort -u | head -15 | sed 's/^/- /')
  fi

  # Write repo file
  KB_FILE="$KB_DIR/${PARENT_DIR}--${REPO_NAME}.md"
  cat > "$KB_FILE" << EOF
# $REPO_NAME

**Group:** $PARENT_DIR
**Languages:** $LANGUAGES
**Path:** $repo

## Structure
\`\`\`
$STRUCTURE
\`\`\`

## Recent Commits
\`\`\`
$COMMITS
\`\`\`

## Key Files
$KEY_FILES
EOF

  if [ -n "$TF_RESOURCES" ]; then
    cat >> "$KB_FILE" << EOF

## Terraform Resources
$TF_RESOURCES
EOF
  fi

  echo "---" >> "$KB_FILE"
  echo "*Auto-indexed: $(date +%Y-%m-%d)*" >> "$KB_FILE"

  ((processed++))
  printf "\r  Indexed: %d/%d" "$processed" "$count"
done

echo ""
echo ""
echo "✓ Knowledge base rebuilt: $processed repos indexed"
echo "  Index: $INDEX_FILE"
echo "  Files: $KB_DIR/"

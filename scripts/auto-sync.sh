#!/bin/bash
# Global repo sync — works in ANY workspace
# Finds all git repos under the current working directory and syncs them

WORKSPACE="${1:-$(pwd)}"
MAX_DEPTH=3
JOBS=8

# Find all git repos (look for .git dirs up to MAX_DEPTH levels deep)
repos=$(find "$WORKSPACE" -maxdepth $MAX_DEPTH -name ".git" -type d 2>/dev/null | sed 's/\/.git$//')

if [ -z "$repos" ]; then
  # If no sub-repos found, check if current dir IS a repo
  if [ -d "$WORKSPACE/.git" ]; then
    repos="$WORKSPACE"
  else
    exit 0
  fi
fi

sync_repo() {
  local repo="$1"
  cd "$repo" || return

  # Fetch with prune (always safe)
  git fetch --prune --quiet 2>/dev/null || return

  # Only pull if on default branch and clean
  local branch=$(git symbolic-ref --short HEAD 2>/dev/null)
  local default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||')
  [ -z "$default" ] && default="main"

  if [ "$branch" = "$default" ] && git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
    git pull --ff-only --quiet 2>/dev/null
  fi
}

export -f sync_repo

# Run syncs in parallel
echo "$repos" | xargs -P $JOBS -I {} bash -c 'sync_repo "$@"' _ {}

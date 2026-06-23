#!/bin/bash
# AWS SSO Login — authenticate to AWS accounts
#
# All profiles sharing the same SSO session are authenticated with one login.
# MCP servers inherit AWS_PROFILE from the environment.
#
# Usage:
#   ~/.claude/scripts/aws-sso-login.sh           # Login (if needed) + verify
#   ~/.claude/scripts/aws-sso-login.sh status    # Check session status
#   ~/.claude/scripts/aws-sso-login.sh profiles  # List configured profiles
#   ~/.claude/scripts/aws-sso-login.sh <profile> # Check specific profile access

set -euo pipefail

# Configured during install — change this to your SSO session name
SSO_SESSION="default"

AWS_CONFIG="${AWS_CONFIG:-$HOME/.aws/config}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

get_profiles() {
  grep -E '^\[profile ' "$AWS_CONFIG" 2>/dev/null | sed 's/\[profile //;s/\]//' | sort
}

get_sso_sessions() {
  grep -E '^\[sso-session ' "$AWS_CONFIG" 2>/dev/null | sed 's/\[sso-session //;s/\]//' | sort
}

is_session_active() {
  local first_profile=$(get_profiles | head -1)
  [ -n "$first_profile" ] && aws sts get-caller-identity --profile "$first_profile" >/dev/null 2>&1
}

check_profile() {
  local profile="$1"
  if aws sts get-caller-identity --profile "$profile" >/dev/null 2>&1; then
    local account=$(aws sts get-caller-identity --profile "$profile" --query Account --output text 2>/dev/null)
    local arn=$(aws sts get-caller-identity --profile "$profile" --query Arn --output text 2>/dev/null)
    echo -e "${GREEN}✓${NC} $profile"
    echo "  Account: $account"
    echo "  Role:    $arn"
    return 0
  else
    echo -e "${RED}✗${NC} $profile — not authenticated"
    return 1
  fi
}

do_login() {
  echo -e "${CYAN}=== AWS SSO Login ===${NC}"
  echo ""

  # Auto-detect SSO session if not configured
  if [ "$SSO_SESSION" = "default" ]; then
    local detected=$(get_sso_sessions | head -1)
    if [ -n "$detected" ]; then
      SSO_SESSION="$detected"
    else
      echo -e "${RED}No SSO session found in ~/.aws/config${NC}"
      echo "Configure one with: aws configure sso"
      exit 1
    fi
  fi

  echo "SSO Session: $SSO_SESSION"
  echo "This will open a browser for authentication."
  echo ""

  aws sso login --sso-session "$SSO_SESSION"

  echo ""
  echo -e "${CYAN}--- Verifying ---${NC}"
  echo ""

  local ok=0
  local fail=0
  for profile in $(get_profiles | head -10); do
    if aws sts get-caller-identity --profile "$profile" >/dev/null 2>&1; then
      ((ok++))
    else
      ((fail++))
    fi
  done

  local total=$((ok + fail))
  if [ $total -gt 0 ]; then
    echo -e "${GREEN}✓${NC} SSO session active — $ok/$total configured profiles accessible"
  fi
  echo ""
  echo "Current AWS_PROFILE: ${AWS_PROFILE:-not set}"
}

show_status() {
  echo -e "${CYAN}=== AWS SSO Status ===${NC}"
  echo ""
  if is_session_active; then
    echo -e "${GREEN}✓ SSO session active${NC}"
    echo ""
    echo "Configured profiles:"
    for profile in $(get_profiles); do
      check_profile "$profile"
      echo ""
    done
    echo "AWS_PROFILE (current): ${AWS_PROFILE:-not set}"
  else
    echo -e "${RED}✗ SSO session expired${NC}"
    echo ""
    echo "Run: ~/.claude/scripts/aws-sso-login.sh"
  fi
}

show_profiles() {
  echo -e "${CYAN}=== Configured AWS Profiles ===${NC}"
  echo ""
  local profiles=$(get_profiles)
  local count=$(echo "$profiles" | grep -c . || echo 0)
  echo "Profiles in ~/.aws/config: $count"
  echo ""
  for profile in $profiles; do
    local account=$(grep -A5 "^\[profile $profile\]" "$AWS_CONFIG" | grep sso_account_id | awk '{print $3}')
    local role=$(grep -A5 "^\[profile $profile\]" "$AWS_CONFIG" | grep sso_role_name | awk '{print $3}')
    echo "  $profile"
    [ -n "$account" ] && echo "    Account: $account | Role: $role"
  done
  echo ""
  echo "Active env: AWS_PROFILE=${AWS_PROFILE:-not set}"
  echo ""
  echo "SSO sessions:"
  get_sso_sessions | sed 's/^/  /'
}

# Main
case "${1:-}" in
  status)
    show_status
    ;;
  profiles)
    show_profiles
    ;;
  "")
    if is_session_active; then
      echo -e "${GREEN}SSO session already active.${NC}"
      echo ""
      show_status
    else
      do_login
    fi
    ;;
  *)
    check_profile "$1" || echo -e "\nRun: ~/.claude/scripts/aws-sso-login.sh"
    ;;
esac

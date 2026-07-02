#!/usr/bin/env bash
# verify_install.sh — Verify that claude-agent-platform installs correctly
# and all entry points are importable.
#
# Usage: ./scripts/verify_install.sh
# Exit codes: 0 = success, 1 = failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

FAILURES=0

info() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAILURES=$((FAILURES + 1)); }

echo "============================================"
echo " CAP Install Verification"
echo "============================================"
echo ""
echo "Project: $PROJECT_DIR"
echo ""

# --- Create temporary venv ---
TMPDIR="${TMPDIR:-/tmp}"
VENV_DIR="$(mktemp -d "${TMPDIR}/cap-verify-XXXXXX")"
trap 'rm -rf "$VENV_DIR"' EXIT

echo "Creating temporary venv at: $VENV_DIR"
python3 -m venv "$VENV_DIR"

# Activate venv
source "$VENV_DIR/bin/activate"

echo "Python: $(python3 --version)"
echo "Pip: $(pip3 --version)"
echo ""

# --- Install package from source ---
echo "Installing claude-agent-platform from source..."
pip3 install --quiet "$PROJECT_DIR" 2>&1 | tail -5 || {
    fail "pip install failed"
    echo ""
    echo "Full install output:"
    pip3 install "$PROJECT_DIR" 2>&1
    exit 1
}
info "Package installed successfully"
echo ""

# --- Test CLI entry point ---
echo "--- Testing CLI entry point ---"
if cap --help >/dev/null 2>&1; then
    info "cap --help works"
else
    fail "cap --help failed"
fi
echo ""

# --- Test all 10 server entry points are importable ---
echo "--- Testing server entry points ---"

SERVERS=(
    "cap.servers.knowledge_server:main"
    "cap.servers.session_server:main"
    "cap.servers.fleet_server:main"
    "cap.servers.workflow_server:main"
    "cap.servers.diagram_server:main"
    "cap.servers.backlog_server:main"
    "cap.servers.ast_server:main"
    "cap.servers.code_intel_server:main"
    "cap.servers.orchestrator_server:main"
    "cap.servers.harness_server:main"
)

for entry in "${SERVERS[@]}"; do
    module="${entry%%:*}"
    attr="${entry##*:}"
    if python3 -c "from ${module} import ${attr}" 2>/dev/null; then
        info "import ${module}.${attr}"
    else
        # Retry with error output for diagnostics
        ERR=$(python3 -c "from ${module} import ${attr}" 2>&1 || true)
        fail "import ${module}.${attr} -- $ERR"
    fi
done
echo ""

# --- Test core modules are importable ---
echo "--- Testing core module imports ---"

CORE_MODULES=(
    "cap.config"
    "cap.db"
    "cap.cli.main"
    "cap.harness.converse_executor"
    "cap.harness.llm_provider"
    "cap.harness.agent_tools"
)

for module in "${CORE_MODULES[@]}"; do
    if python3 -c "import ${module}" 2>/dev/null; then
        info "import ${module}"
    else
        ERR=$(python3 -c "import ${module}" 2>&1 || true)
        fail "import ${module} -- $ERR"
    fi
done
echo ""

# --- Test data files are included in the installed package ---
echo "--- Testing package data inclusion ---"

DATA_CHECK=$(python3 -c "
import importlib.resources
import cap.data
from pathlib import Path

# Check agents directory exists and has .md files
data_path = Path(cap.data.__file__).parent
agents_dir = data_path / 'agents'
if agents_dir.exists():
    md_files = list(agents_dir.glob('*.md'))
    if len(md_files) > 0:
        print(f'OK:{len(md_files)} agent definition files found')
    else:
        print('FAIL:agents dir exists but no .md files')
else:
    print('FAIL:agents directory not found in installed package')
" 2>&1)

if [[ "$DATA_CHECK" == OK:* ]]; then
    info "${DATA_CHECK#OK:}"
else
    fail "${DATA_CHECK#FAIL:}"
fi
echo ""

# --- Test optional dependencies are importable when installed ---
echo "--- Testing optional dependency availability ---"

# These should be importable since they are in core deps
OPTIONAL_CHECKS=(
    "langchain_core:langchain-core"
    "langchain_aws:langchain-aws"
    "langgraph:langgraph"
)

for check in "${OPTIONAL_CHECKS[@]}"; do
    module="${check%%:*}"
    pkg="${check##*:}"
    if python3 -c "import ${module}" 2>/dev/null; then
        info "${pkg} importable"
    else
        fail "${pkg} NOT importable (required dependency)"
    fi
done
echo ""

# --- Summary ---
echo "============================================"
if [ "$FAILURES" -eq 0 ]; then
    echo -e "${GREEN}ALL CHECKS PASSED${NC}"
    echo "============================================"
    exit 0
else
    echo -e "${RED}${FAILURES} CHECK(S) FAILED${NC}"
    echo "============================================"
    exit 1
fi

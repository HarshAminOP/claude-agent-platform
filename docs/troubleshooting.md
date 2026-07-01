# Troubleshooting

## Diagnostic Commands

```bash
cap status          # Overall health check
cap doctor          # Detailed diagnostics
cap db-doctor       # Database integrity
cap fleet status    # MCP server health
cap fleet health-check  # Force health check
```

## Common Issues

### `cap: command not found`

**Cause:** `cap` binary not on PATH after install.

**Fix:**
```bash
uv tool update-shell
source ~/.zshrc  # or ~/.bashrc
```

If using pip:
```bash
pip install --user claude-agent-platform
export PATH="$HOME/.local/bin:$PATH"
```

---

### MCP servers not responding

**Cause:** Servers not registered or crashed.

**Fix:**
```bash
# Check server status
cap fleet status

# Re-register servers
cap init --force

# Check if Claude Code sees them
cat ~/.claude.json | python3 -m json.tool | grep cap-
```

---

### `NoCredentialsError` / `ExpiredTokenException`

**Cause:** AWS credentials missing or expired.

**Fix:**
```bash
# For SSO:
aws sso login --sso-session <your-session>

# Verify:
aws sts get-caller-identity --profile <your-profile>

# For env vars, ensure they're set:
echo $AWS_ACCESS_KEY_ID
```

---

### `AccessDeniedException` on Bedrock

**Cause:** IAM policy does not allow `bedrock:InvokeModel`.

**Fix:**
1. Check IAM policy allows `bedrock:InvokeModel` on Claude models
2. Ensure model access is enabled in Bedrock console
3. Verify the region has the requested model available

```bash
# Test access directly:
aws bedrock invoke-model \
  --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' \
  --region us-east-1 \
  output.json
```

---

### Database corruption

**Cause:** Unexpected process termination, disk full, or file permission issues.

**Fix:**
```bash
# Check integrity
cap db-doctor

# Fix automatically
cap db-doctor --fix --yes

# If unfixable, restore from backup
cap restore
```

---

### Knowledge search returns no results

**Cause:** Workspace not indexed.

**Fix:**
```bash
# Check index status
cap knowledge status

# Force full re-index
cap sync --full --workspace /path/to/your/project

# Verify
cap knowledge search "test query"
```

---

### Semantic search unavailable (keyword-only mode)

**Cause:** No AWS credentials for Titan Embeddings, or embedding model not enabled.

**Fix:**
- This is expected behavior without Bedrock access
- CAP automatically uses keyword + graph search as fallback
- To enable: configure Bedrock credentials and enable Titan Embed Text V2

```bash
# Check if embeddings are working
cap knowledge status
# Look for: "Embedding status: degraded" or "no_credentials"
```

---

### Budget exceeded / workflow killed

**Cause:** Daily or monthly budget cap reached.

**Fix:**
```bash
# Check current spend
cap budget status

# Raise daily limit
cap budget raise 10.0

# Resume after pause
cap budget resume
```

---

### `cap init` hangs during model probe

**Cause:** Network timeout reaching Bedrock, or missing credentials.

**Fix:**
```bash
# Skip model probe (use defaults)
cap init --skip-fetch

# Or use non-interactive mode
cap init --non-interactive
```

---

### Agent produces low-quality output

**Cause:** Wrong model tier assigned, or insufficient context.

**Fix:**
1. Check agent-to-model mapping in harness-config.json
2. Ensure knowledge base is indexed for your workspace
3. Verify session memory is loading (`cap session list`)

```bash
# Verify config
cat ~/.claude-platform/harness-config.json | python3 -m json.tool
```

---

### WAL file growing too large

**Cause:** Background maintenance not running (checkpoint not triggered).

**Fix:**
```bash
# Manual checkpoint
cap db-doctor --fix --yes

# Start daemon for automatic maintenance
cap daemon start
```

---

### Permission denied on database files

**Cause:** File permissions changed (should be 0600).

**Fix:**
```bash
chmod 600 ~/.claude-platform/*.db
chmod 600 ~/.claude-platform/*.db-wal 2>/dev/null
chmod 600 ~/.claude-platform/*.db-shm 2>/dev/null
```

Or:
```bash
cap db-doctor --fix --yes
```

---

### `cap uninstall` failed to restore configs

**Cause:** Backup files missing or corrupted.

**Fix:**
```bash
# Check for backups
ls ~/.claude-platform/backups/

# Manual restore of Claude Code config
# (remove CAP MCP server entries from ~/.claude.json)
```

---

### GitHub auto-clone fails

**Cause:** SSH key not configured, org access denied, or rate limit.

**Fix:**
```bash
# Test SSH access
ssh -T git@github.com

# Test gh CLI
gh auth status
gh repo view your-org/some-repo

# Check config
cap github config --show
```

---

## Log Locations

| Component | Log Location |
|:----------|:-------------|
| MCP servers | stderr (visible in Claude Code developer tools) |
| CLI commands | stdout/stderr |
| Daemon | `~/.claude-platform/logs/daemon.log` |
| Health checks | `cap fleet status` shows recent events |

## Getting Help

```bash
# Version info
cap --version

# Command help
cap <command> --help

# Full diagnostics
cap doctor
cap status
cap fleet status
cap knowledge status
cap budget status
```

## Reset Everything

If all else fails, full reset:

```bash
cap uninstall --yes
rm -rf ~/.claude-platform
cap init
```

This destroys all knowledge, session memory, and configuration. Use as a last resort.

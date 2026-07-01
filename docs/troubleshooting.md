### Troubleshooting

Common issues and resolutions. Organized by symptom.

#### Embedding Failures

**Symptom**: `cap embed` fails or `knowledge status` shows 0% embedding coverage.

**Causes and fixes**:

1. **SCP blocks Titan model**:
   - Error: `AccessDeniedException` or `UnrecognizedClientException`
   - Fix: Request model access in Bedrock console, or switch to sentence-transformers fallback
   - Fallback config:
   ```json
   { "embeddings": { "fallback": "sentence-transformers" } }
   ```

2. **Wrong region**:
   - Error: model not available in configured region
   - Fix: `cap config set bedrock.region us-east-1`

3. **Credential issues**:
   - Error: `ExpiredTokenException`
   - Fix: `aws sso login --sso-session <your-session>`

4. **sentence-transformers not installed**:
   - Error: `ModuleNotFoundError: No module named 'sentence_transformers'`
   - Fix: `pip install sentence-transformers`

#### Budget Exceeded

**Symptom**: agents stop executing, "budget exceeded" errors.

**Diagnosis**:
```bash
cap budget status       # Check current spend
cap budget history      # Check daily trend
```

**Fixes**:
- Temporary: `cap budget raise 5.0` (adds $5 to daily cap)
- Resume after pause: `cap budget resume`
- Reset counter: `cap budget reset --yes`
- Permanent: `cap config set budget.monthly_cap_usd 100`

#### Agent Coordination Failures

**Symptom**: workflows fail mid-execution, steps stuck in RUNNING state.

**Diagnosis**:
```bash
cap orch-status         # Check circuit breakers, DLQ
cap health              # Per-agent failure rates
cap dlq list            # View failed tasks
```

**Fixes**:
- Retry failed tasks: `cap dlq retry <task-id>` or `cap dlq retry-all`
- Dismiss unfixable: `cap dlq dismiss <task-id>`
- Circuit breaker open: wait for cooldown (2min) or check underlying cause
- Kill stuck workflow: `cap workflow kill <workflow-id>`

#### Knowledge Graph Empty

**Symptom**: `cap index graph --stats` shows 0 nodes/edges.

**Causes and fixes**:

1. **Never indexed**: run `cap index run --workspace /path/to/code`
2. **No repos detected**: ensure workspace contains repo markers (.git, go.mod, etc.)
3. **All repos unchanged**: use `cap index run --full` to force re-index
4. **Indexer budget exhausted**: check `cap index status` and raise budget

#### MCP Servers Not Responding

**Symptom**: Claude Code cannot use CAP tools.

**Diagnosis**:
```bash
cap fleet status        # Check server states
cap fleet health-check  # Run health probe
```

**Fixes**:
- Servers not registered: `cap init` (re-registers all servers)
- Servers crashed: restart via fleet or `cap init --force`
- Check ~/.claude.json has cap-* entries

#### Database Corruption

**Symptom**: SQLite errors, "database is malformed".

**Diagnosis and fix**:
```bash
cap db-doctor           # Diagnose issues
cap db-doctor --fix --yes  # Apply fixes
```

If unrecoverable:
```bash
cap restore             # Restore from backup
```

#### Init Fails

**Symptom**: `cap init` errors out partway through.

**Common causes**:
- Python < 3.11: upgrade Python
- Missing tomli: `pip install tomli`
- Permission denied: check ~/.claude-platform/ permissions
- claude CLI not found: install Claude Code CLI first

**Recovery**:
```bash
cap init --force        # Retry with force (overwrites existing)
```

#### Slow Indexing

**Symptom**: `cap index run` takes very long.

**Optimizations**:
- Skip LLM analysis: `cap index run --skip-llm`
- Skip embeddings: `cap index run --skip-embeddings`
- Reduce concurrency if rate-limited: `--concurrency 1`
- Use incremental (default) instead of `--full`
- Exclude large repos via `indexing.exclude_patterns`

#### Validation Checklist

After setup, verify everything works:
```bash
cap status              # All DBs healthy, servers registered
cap budget status       # Budget tracking active
cap knowledge status    # Entries indexed, coverage > 0%
cap fleet health-check  # All servers healthy
cap index status        # Indexer has run at least once
```

#### Cross-links
Link to: [Installation](installation.md), [Configuration](configuration.md), [CLI Reference](cli-reference.md)

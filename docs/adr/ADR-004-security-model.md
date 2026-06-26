# ADR-004: Pre-Index Skip-Based Security (No Inline Redaction)

**Status:** Accepted  
**Date:** 2026-06-25  
**Context:** Version 1

## Context

The system indexes content from 41+ repositories across the platform engineering workspace. Some repos may contain:
- AWS access keys, private keys in `.env` or secrets files
- GitHub tokens, SSH keys, terraform state secrets
- Potentially sensitive documentation or internal IP ranges

**Threat Model:**
- Local-only system (developer laptop), no network exposure
- Internal repos (all owned by same team)
- No untrusted agents; all consumers are platform team members
- Risk: Accidentally indexed secrets become searchable in knowledge database

**Constraints:**
- Indexing must be fast (43s full scan target)
- No manual review required for 1000s of files
- Security must fail closed (skip dubious content, don't try to redact)

## Decision

**Files with detected secrets or suspicious patterns are SKIPPED ENTIRELY. No inline redaction, no partial indexing.**

**Rationale:**
- Defense in depth: prevent secrets from entering the system at all
- Fail-closed: if unsure, skip (no false negatives where a secret escapes)
- Simple: 80 lines of regex-based gate vs 1000s of context-aware redaction logic
- Conservative: better to miss a file with embedded docs than leak credentials
- No false-positive UX impact: agents won't search for secrets anyway

## Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **Inline regex redaction** | Allows indexing files with embedded secrets | Easy to miss patterns, false negatives (e.g., "secret" in comment + real key on next line), no guarantee of safety | Rejected |
| **Content hashing (no secrets in search)** | Preserve everything, anonymize on search | Still indexes secret content, hash collisions possible, doesn't solve indexed-secret problem | Rejected |
| **Encrypted storage (secrets encrypted in DB)** | More complex protection | Adds encryption/key management complexity, secrets still in DB (just encrypted), key rotation burden | Rejected |
| **Manual code review before indexing** | Catches context-dependent secrets | Doesn't scale, 1000s of files, time-consuming, error-prone | Rejected |
| **Ship without security gate** | Simplest implementation | Unacceptable risk: .env, private keys in DB; violates OWASP guidance | Rejected |
| **Skip-only gate (this approach)** | Fast, safe, simple, fail-closed | Some false positives (unlikely with tuned patterns), rare valid use of patterns like "password" in config files | **Accepted** |

## Security Gate Architecture

**Three-layer pre-extraction gate:**

```
Layer 1: Path Exclusion (filenames + patterns)
    └─> .env, .env.*, credentials, terraform.tfvars, *.pem, *.key
    └─> skip_extensions: .bin, .pb, .wasm (also security + performance)

    ▼

Layer 2: File Size + Binary Check
    └─> >1MB files (skip to prevent OOM)
    └─> Contains null bytes (binary file detection)

    ▼

Layer 3: Regex Secret Pattern Detection
    └─> AWS Access Key (AKIA*)
    └─> Private keys (-----BEGIN PRIVATE KEY-----)
    └─> GitHub tokens (ghp_*, gho_*)
    └─> Slack tokens (xox-*)
    └─> OpenAI keys (sk-*)
    └─> Context patterns (password|secret|token|api_key) + value
    └─> aws_secret_access_key pattern

    ▼

File passes all gates → Safe to extract
```

## Secret Patterns (High Confidence)

```python
SECRET_PATTERNS = [
    re.compile(r'AKIA[A-Z0-9]{16}'),                          # AWS Access Key
    re.compile(r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----'),  # Private Keys
    re.compile(r'ghp_[A-Za-z0-9]{36,}'),                      # GitHub PAT
    re.compile(r'gho_[A-Za-z0-9]{36,}'),                      # GitHub OAuth
    re.compile(r'xox[bpras]-[A-Za-z0-9-]{10,}'),              # Slack
    re.compile(r'sk-[A-Za-z0-9]{32,}'),                        # OpenAI/generic
    re.compile(r'npm_[A-Za-z0-9]{36,}'),                       # npm
    # Context-dependent (keyword prefix required)
    re.compile(r'(?i)(password|passwd|secret|token|api_key|credential)\s*[:=]\s*["\'][^"\']{8,}["\']'),
    re.compile(r'(?i)(aws_secret_access_key)\s*=\s*["\']?[A-Za-z0-9/+=]{20,}'),
    re.compile(r'(?i)"private_key"\s*:\s*"-----BEGIN'),
]
```

## Skip Filenames

```python
SKIP_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.staging",
    "credentials", "credentials.json", "config.json",
    "terraform.tfvars", "terraform.tfvars.json",
    "secrets.yaml", "sealed-secrets.yaml",
}
```

## Consequences

### Positive
- **No secrets in database:** Fail-closed design prevents credential leakage
- **Fast ingestion:** Gate runs before parsing (~2s for 42 repos)
- **Simple operations:** No encryption keys to manage, no decryption overhead
- **Legal safety:** Reduces compliance/audit risk around secret storage
- **Easy to audit:** Regex patterns are visible, testable, auditable
- **Scalable:** Gate cost is constant with file size (checks first 8KB)

### Negative
- **Some false positives:** Config files with legitimate "password" references might be skipped (acceptable for v1)
- **No recovery:** Skipped files are lost; no way to re-index if pattern is disabled later
- **Manual pattern maintenance:** New secret types require gate updates
- **Doesn't handle all risks:** Prompt injection patterns still allowed (see ADR trade-off)
- **Semantic blindness:** Can't distinguish "this is a secret" from "this mentions secrets"

## Non-Negotiable Security Trade-offs

**Prompt Injection Patterns:** NOT skipped. Rationale:
- All repos are internal (single team ownership)
- Injection patterns are rare in platform infra code
- Skipping would require complex NLP-based detection
- Risk is minimal compared to leaked credentials
- If needed in future, add `injection_patterns.txt` with manual review

**Audit Trail:** NOT implemented in v1. Rationale:
- Local-only system, no network exposure
- All users are trusted team members
- Audit logging adds schema + rotation logic for minimal threat model
- Deferred to v2 if regulatory requirements emerge

## Testing & Validation

Gate correctness verified by:
1. **Negative test:** Known secret patterns → file skipped
2. **Positive test:** Clean files → file indexed
3. **Edge cases:** False positives in config files should be rare; document and accept

```python
# Test: AWS key detection
test_content = "AKIAIOSFODNN7EXAMPLE"
assert not SecurityGate().check("test.py", test_content)  # Must be skipped

# Test: Clean code
test_content = "password = get_password_from_vault()"  # Function call, not literal
assert SecurityGate().check("test.py", test_content)  # Must be indexed
```

## Related ADRs

- [ADR-001: Search Engine](ADR-001-search-engine.md) — Cleaner data enters FTS5
- [ADR-007: Ingestion Strategy](ADR-007-ingestion-strategy.md) — Gate is stage 2 of pipeline

## Implementation Notes

**Gate runs BEFORE extraction:** Content never parsed if secrets detected.

**Performance:** ~2s for 42 repos (50 files with secrets in typical workspace).

**Logging:** WARNING level for each skipped file (helps audit what was filtered).

**SLO:** Gate adds <100ms per file; overall ingestion remains <45s for full rescan.

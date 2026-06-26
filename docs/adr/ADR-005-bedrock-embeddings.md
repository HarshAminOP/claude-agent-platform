# ADR-005: Bedrock Titan V2 for Embeddings (replaces local ONNX)

## Status
Accepted

## Context
The platform needs vector embeddings for semantic search. Two options:
1. Local ONNX runtime with all-MiniLM-L6-v2 (384 dims, 512 token context, 22MB model file)
2. AWS Bedrock Titan Text Embeddings V2 (1024 dims, 8K context, API call)

## Decision
Use `amazon.titan-embed-text-v2:0` via Bedrock API.

## Rationale
- Same AWS auth already configured for Claude — zero new credentials
- Higher quality: 1024 dimensions vs 384, 8K context vs 512 tokens
- Zero local dependencies: no ONNX runtime, no model file download
- Cost negligible: $0.02 per 1M tokens (platform indexes ~10K entities = pennies)
- Batch support: up to 2048 texts per API call
- Already in the corporate Bedrock endpoint

## Trade-offs
- Network latency (~30ms vs ~10ms local) — irrelevant for batch ingest
- Requires AWS connectivity — fallback to FTS5-only mode when offline
- Bedrock rate limits apply — uses adaptive pool (haiku-weight slot)

## Supersedes / Related Decisions

This decision **accelerates the v2 deferral from ADR-001**. Instead of deferring vector search to a future phase, Bedrock embeddings are implemented in parallel with FTS5, enabling hybrid retrieval from day-1.

- **ADR-001** proposed deferring vector search pending real usage data
- **ADR-005** replaces that deferral by making embeddings a first-class feature via AWS Bedrock

## Consequences
- Remove ONNX from requirements.txt
- Embeddings client is a thin boto3 wrapper (~50 lines)
- LanceDB stores vectors locally (no server needed)
- Graceful degradation: FTS5 always works, semantic search requires Bedrock

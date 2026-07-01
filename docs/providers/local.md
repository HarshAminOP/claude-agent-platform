# Local Provider (Ollama)

## Overview

Run CAP with locally-hosted models via Ollama or any OpenAI-compatible API server. Best for offline use, experimentation, or environments without cloud access.

## Install Ollama

### macOS

```bash
brew install ollama
```

### Linux

```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### Start Ollama

```bash
ollama serve
```

Ollama runs on `http://localhost:11434` by default.

## Pull Models

Pull at least one model before initializing CAP:

```bash
# Recommended for each tier:
ollama pull llama3           # haiku tier (fast, small)
ollama pull codestral        # sonnet tier (code-focused)
ollama pull llama3:70b       # opus tier (large, capable)
```

## Initialize CAP

```bash
cap init
# Select: local
# Enter base URL: http://localhost:11434 (default)
# Map models to tiers
```

## Configuration

In `~/.claude-platform/harness-config.json`:

```json
{
  "provider": "local",
  "local": {
    "base_url": "http://localhost:11434",
    "models": {
      "haiku": "llama3",
      "sonnet": "codestral",
      "opus": "llama3:70b"
    }
  }
}
```

## Compatible Models

### Recommended Model Mapping

| Tier | Recommended Model | Size | Notes |
|:-----|:------------------|:-----|:------|
| haiku | `llama3` (8B) | 4.7 GB | Fast responses, basic tasks |
| sonnet | `codestral` (22B) | 13 GB | Code-focused, good for development |
| opus | `llama3:70b` | 40 GB | Most capable, needs 64GB+ RAM |

### Alternative Models

| Model | Size | Good For |
|:------|:-----|:---------|
| `mistral` (7B) | 4.1 GB | General purpose, fast |
| `deepseek-coder` (6.7B) | 3.8 GB | Code generation |
| `mixtral` (47B) | 26 GB | Multi-task, high quality |
| `qwen2.5-coder` (7B) | 4.4 GB | Code generation |
| `phi3` (3.8B) | 2.3 GB | Very fast, limited capability |

### Memory Requirements

| Model Size | RAM Needed | GPU VRAM |
|:-----------|:-----------|:---------|
| 7B | 8 GB | 6 GB |
| 13B | 16 GB | 10 GB |
| 22B | 24 GB | 16 GB |
| 70B | 64 GB | 48 GB |

## Limitations

The local provider has significant limitations compared to Bedrock or Anthropic API:

| Limitation | Impact |
|:-----------|:-------|
| No tool use | Multi-turn tool interactions not supported |
| Lower quality | Local models produce less accurate results |
| No embeddings | Semantic search unavailable (keyword + graph only) |
| No streaming | Responses arrive all at once |
| Context window | Typically 4K-32K vs 200K for Claude |
| Speed | CPU inference is slow for large models |

### What Works Well

- Simple code generation
- Documentation writing
- Basic code review
- Knowledge base search (keyword mode)
- Quick lookups and status checks

### What Does Not Work Well

- Complex multi-agent workflows
- Architecture decisions
- Security analysis
- Tasks requiring tool use / function calling

## Custom API Servers

CAP works with any OpenAI-compatible API server:

### vLLM

```bash
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-3-8b-chat-hf \
  --port 8000
```

```json
{
  "local": {
    "base_url": "http://localhost:8000",
    "models": {
      "haiku": "meta-llama/Llama-3-8b-chat-hf"
    }
  }
}
```

### LM Studio

1. Start LM Studio server
2. Load a model
3. Set base URL to `http://localhost:1234`

## Troubleshooting

| Error | Cause | Fix |
|:------|:------|:----|
| Connection refused | Ollama not running | Run `ollama serve` |
| Model not found | Model not pulled | Run `ollama pull <model>` |
| Out of memory | Model too large | Use a smaller model |
| Slow responses | CPU inference | Use GPU or smaller model |
| Garbled output | Incompatible model | Switch to a supported model |

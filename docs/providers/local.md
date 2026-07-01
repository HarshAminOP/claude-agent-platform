### Local Provider (Ollama)

Fully local operation. Uses `langchain-ollama` ChatOllama. No cloud dependency, no cost, no auth.

#### Setup

1. Install Ollama: https://ollama.com
2. Pull a model with tool-calling support:
```bash
ollama pull qwen2.5:14b
```
3. Verify Ollama is running:
```bash
ollama list
```

4. Configure in harness-config.json:
```json
{
  "provider": "local",
  "local": {
    "url": "http://localhost:11434",
    "model": "qwen2.5:14b"
  }
}
```

#### Available Models (tool-calling capable)

Only models with function/tool calling support work with CAP:

| Model | Size | Tool Calling | Notes |
|-------|------|-------------|-------|
| qwen2.5:14b | 14B | Yes | Recommended for local use |
| qwen2.5:7b | 7B | Yes | Faster, less capable |
| llama3.1:8b | 8B | Yes | Good general purpose |
| mistral-nemo | 12B | Yes | Strong reasoning |
| command-r | 35B | Yes | High quality, needs RAM |

Models WITHOUT tool calling (will NOT work): llama2, codellama, phi-2, gemma.

#### Embedding: sentence-transformers

Always uses local embeddings with the local provider:
- Model: `sentence-transformers/all-MiniLM-L6-v2`
- Dimensions: 384
- Runs in-process (Python)
- No external API calls
- Install: `pip install sentence-transformers`

#### Performance Considerations

- Local models are significantly slower than cloud APIs
- RAM requirements: 7B models need ~8GB, 14B need ~16GB, 35B need ~32GB
- GPU acceleration recommended but not required
- Tool calling accuracy varies by model — qwen2.5 is most reliable
- Not recommended for production workflows or large indexing runs

#### When to Use

- Offline/airgapped environments
- Cost-free development and testing
- Privacy-sensitive workloads
- Learning/experimenting with CAP

#### Cross-links
Link to: [Configuration](../configuration.md), [Bedrock Provider](bedrock.md)

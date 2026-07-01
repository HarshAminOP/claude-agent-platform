"""Multi-turn Converse executor with tool use support.

Supports multiple LLM providers:
- aws-bedrock: Bedrock Converse API (default)
- anthropic-api: Direct Anthropic Messages API
- azure-openai: Azure OpenAI (not yet implemented)
- local: Local models via OpenAI-compatible API (not yet implemented)

Uses the Bedrock Converse API for multi-turn conversations where agents
can call tools (file_read, bash_exec, knowledge_search) iteratively
until they produce a final answer.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError

logger = logging.getLogger("cap.harness.converse_executor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOOL_ITERATIONS = 15  # Max tool-use round-trips per execution
MAX_RETRIES = 2  # Max retries on throttle
BACKOFF_BASE_S = 1.0  # Initial backoff delay
BACKOFF_MULTIPLIER = 4.0  # Multiplier per retry
DEFAULT_MAX_TOKENS = 8192
TOOL_OUTPUT_MAX_CHARS = 50_000  # Truncate tool outputs to prevent token explosion

# Model aliases (same as executor.py — import from there)
from cap.harness.executor import (
    MODEL_ALIASES,
    MODEL_PRICING,
    _resolve_model,
    _tier_for_model_id,
    _compute_cost,
    ExecutionResult,
)


# ---------------------------------------------------------------------------
# Agent Definition Loader
# ---------------------------------------------------------------------------

AGENT_DEFS_DIR = Path(__file__).parent.parent / "data" / "agents"


def load_agent_system_prompt(agent_type: str) -> Optional[str]:
    """Load system prompt from the agent definition .md file.

    Parses YAML frontmatter (between --- markers) and returns the
    markdown body as the system prompt.
    """
    md_path = AGENT_DEFS_DIR / f"{agent_type}.md"
    if not md_path.exists():
        return None

    content = md_path.read_text(encoding="utf-8")

    # Strip YAML frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()

    return content.strip()


# ---------------------------------------------------------------------------
# Tool Definitions (Bedrock Converse format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "file_read",
            "description": "Read the contents of a file at the given absolute path. Returns the file content as text.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute file path to read",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Line number to start reading from (0-indexed). Optional.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to read. Optional.",
                        },
                    },
                    "required": ["path"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "bash_exec",
            "description": "Execute a bash command and return stdout/stderr. Use for running tests, linting, searching, or any CLI operation. Commands have a 60-second timeout.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory for the command. Optional.",
                        },
                    },
                    "required": ["command"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "knowledge_search",
            "description": "Search the CAP knowledge base for relevant information about repos, services, patterns, and conventions.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["all", "code", "config", "doc"],
                            "description": "Scope to search within. Default: all",
                        },
                    },
                    "required": ["query"],
                }
            },
        }
    },
]


# ---------------------------------------------------------------------------
# Tool Execution
# ---------------------------------------------------------------------------

def _execute_file_read(input_data: dict) -> str:
    """Read a file and return its content."""
    path = input_data.get("path", "")
    if not path or not os.path.isabs(path):
        return f"Error: path must be absolute. Got: {path!r}"

    if not os.path.exists(path):
        return f"Error: file not found: {path}"

    if not os.path.isfile(path):
        return f"Error: not a regular file: {path}"

    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        offset = input_data.get("offset", 0)
        limit = input_data.get("limit")

        lines = content.splitlines(keepends=True)
        if offset and offset > 0:
            lines = lines[offset:]
        if limit and limit > 0:
            lines = lines[:limit]

        result = "".join(lines)
        if len(result) > TOOL_OUTPUT_MAX_CHARS:
            result = result[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
        return result
    except Exception as exc:
        return f"Error reading file: {exc}"


def _execute_bash(input_data: dict) -> str:
    """Execute a bash command with timeout."""
    command = input_data.get("command", "")
    if not command:
        return "Error: command is required"

    cwd = input_data.get("cwd")
    if cwd and not os.path.isdir(cwd):
        return f"Error: working directory does not exist: {cwd}"

    # Security: block obviously dangerous commands
    dangerous = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", ":(){ :|:& };:"]
    lower_cmd = command.lower()
    for d in dangerous:
        if d in lower_cmd:
            return f"Error: blocked dangerous command pattern: {d}"

    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"

        if not output.strip():
            output = "[no output]"

        if len(output) > TOOL_OUTPUT_MAX_CHARS:
            output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
        return output
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 60 seconds"
    except Exception as exc:
        return f"Error executing command: {exc}"


def _execute_knowledge_search(input_data: dict) -> str:
    """Search the knowledge base."""
    query = input_data.get("query", "")
    if not query:
        return "Error: query is required"

    scope = input_data.get("scope", "all")

    try:
        # Try to use the knowledge search module
        from cap.knowledge.search import search as kb_search  # type: ignore
        results = kb_search(query, scope=scope, top_k=5)
        if not results:
            return "No results found."

        output_parts = []
        for r in results[:5]:
            title = r.get("title", "untitled")
            preview = r.get("content_preview", r.get("content", ""))[:300]
            source = r.get("source_path", "unknown")
            output_parts.append(f"## {title}\nSource: {source}\n{preview}\n")
        return "\n---\n".join(output_parts)
    except ImportError:
        return "Knowledge base not available (module not found)"
    except Exception as exc:
        return f"Knowledge search error: {exc}"


# Tool dispatcher
TOOL_HANDLERS: dict[str, Callable[[dict], str]] = {
    "file_read": _execute_file_read,
    "bash_exec": _execute_bash,
    "knowledge_search": _execute_knowledge_search,
}


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call to the appropriate handler."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: unknown tool '{tool_name}'"
    return handler(tool_input)


# ---------------------------------------------------------------------------
# Conversation Result
# ---------------------------------------------------------------------------

@dataclass
class ConversationResult:
    """Result of a multi-turn conversation execution."""

    agent_id: str
    agent_type: str
    model: str
    response: Optional[str]
    error: Optional[str]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    duration_ms: int
    turns: int  # Number of API calls made
    tool_calls: list = field(default_factory=list)  # Record of tool invocations
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_execution_result(self) -> ExecutionResult:
        """Convert to the legacy ExecutionResult format for backward compat."""
        return ExecutionResult(
            agent_id=self.agent_id,
            model=self.model,
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
            cost_usd=self.total_cost_usd,
            duration_ms=self.duration_ms,
            response=self.response,
            error=self.error,
            timestamp=self.timestamp,
        )


# ---------------------------------------------------------------------------
# Provider Abstraction Layer
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """Thin wrapper translating Converse-style calls to Anthropic Messages API.

    This allows the ConverseExecutor to use the direct Anthropic SDK
    when configured with provider=anthropic-api. The API key is resolved
    from an environment variable (never stored in config).
    """

    def __init__(self, api_key: str) -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for provider=anthropic-api. "
                "Install it with: pip install anthropic"
            )
        self._client = anthropic.Anthropic(api_key=api_key)

    def converse(
        self,
        model_id: str,
        messages: list,
        system: list | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        tool_config: dict | None = None,
    ) -> dict:
        """Translate a Bedrock Converse-style call to Anthropic Messages API.

        Converts:
        - system: [{"text": "..."}] -> system="..."
        - messages: Bedrock format -> Anthropic format
        - toolConfig: Bedrock format -> Anthropic tools format
        - Response: Anthropic format -> Bedrock Converse response format
        """
        # Build system prompt
        system_text = None
        if system:
            system_text = "\n".join(s.get("text", "") for s in system)

        # Convert messages from Bedrock to Anthropic format
        anthropic_messages = self._convert_messages_to_anthropic(messages)

        # Convert tool config
        tools = None
        if tool_config and "tools" in tool_config:
            tools = self._convert_tools_to_anthropic(tool_config["tools"])

        # Make the API call
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_text:
            kwargs["system"] = system_text
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        # Convert response back to Bedrock Converse format
        return self._convert_response_to_converse(response)

    def converse_stream(
        self,
        model_id: str,
        messages: list,
        system: list | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
    ) -> dict:
        """Streaming call translated to Anthropic streaming API.

        Returns a dict with a 'stream' key containing an iterable of events
        in Bedrock Converse stream format.
        """
        system_text = None
        if system:
            system_text = "\n".join(s.get("text", "") for s in system)

        anthropic_messages = self._convert_messages_to_anthropic(messages)

        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_text:
            kwargs["system"] = system_text

        # Use streaming
        with self._client.messages.stream(**kwargs) as stream:
            events = []
            text_parts = []
            for text in stream.text_stream:
                text_parts.append(text)
                events.append({
                    "contentBlockDelta": {
                        "delta": {"text": text}
                    }
                })

            # Get final message for token usage
            final_message = stream.get_final_message()
            events.append({
                "metadata": {
                    "usage": {
                        "inputTokens": final_message.usage.input_tokens,
                        "outputTokens": final_message.usage.output_tokens,
                    }
                }
            })

        return {"stream": events}

    def _convert_messages_to_anthropic(self, messages: list) -> list:
        """Convert Bedrock Converse messages to Anthropic format."""
        result = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", [])

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            # Convert content blocks
            anthropic_content = []
            for block in content:
                if "text" in block:
                    anthropic_content.append({"type": "text", "text": block["text"]})
                elif "toolUse" in block:
                    tu = block["toolUse"]
                    anthropic_content.append({
                        "type": "tool_use",
                        "id": tu["toolUseId"],
                        "name": tu["name"],
                        "input": tu.get("input", {}),
                    })
                elif "toolResult" in block:
                    tr = block["toolResult"]
                    tool_content = []
                    for tc in tr.get("content", []):
                        if "text" in tc:
                            tool_content.append({"type": "text", "text": tc["text"]})
                    anthropic_content.append({
                        "type": "tool_result",
                        "tool_use_id": tr["toolUseId"],
                        "content": tool_content,
                    })

            result.append({"role": role, "content": anthropic_content})

        return result

    def _convert_tools_to_anthropic(self, bedrock_tools: list) -> list:
        """Convert Bedrock toolConfig tools to Anthropic tools format."""
        tools = []
        for tool_def in bedrock_tools:
            spec = tool_def.get("toolSpec", {})
            schema = spec.get("inputSchema", {}).get("json", {})
            tools.append({
                "name": spec["name"],
                "description": spec.get("description", ""),
                "input_schema": schema,
            })
        return tools

    def _convert_response_to_converse(self, response: Any) -> dict:
        """Convert Anthropic Messages response to Bedrock Converse format."""
        content_blocks = []
        for block in response.content:
            if block.type == "text":
                content_blocks.append({"text": block.text})
            elif block.type == "tool_use":
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                })

        # Map stop_reason to Bedrock stopReason
        stop_reason_map = {
            "end_turn": "end_turn",
            "max_tokens": "max_tokens",
            "tool_use": "tool_use",
        }
        stop_reason = stop_reason_map.get(response.stop_reason, "end_turn")

        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": content_blocks,
                }
            },
            "stopReason": stop_reason,
            "usage": {
                "inputTokens": response.usage.input_tokens,
                "outputTokens": response.usage.output_tokens,
            },
        }


def _create_provider_client(config: dict | None = None):
    """Factory method to create the appropriate LLM client based on provider config.

    Returns:
    - For aws-bedrock: None (ConverseExecutor creates boto3 client internally)
    - For anthropic-api: AnthropicProvider instance
    - For azure-openai: raises NotImplementedError
    - For local: raises NotImplementedError

    Parameters
    ----------
    config:
        Harness config dict. If None, loaded from harness_config.
    """
    if config is None:
        from cap.lib.harness_config import load_harness_config
        config = load_harness_config()

    provider = config.get("provider", "aws-bedrock")

    if provider == "aws-bedrock":
        # ConverseExecutor handles boto3 client creation internally
        return None

    elif provider == "anthropic-api":
        from cap.lib.harness_config import get_anthropic_api_key
        api_key = get_anthropic_api_key(config)
        if not api_key:
            anthropic_cfg = config.get("anthropic", {})
            env_var = anthropic_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
            raise ValueError(
                f"Anthropic API key not found. Set the ${env_var} environment variable."
            )
        return AnthropicProvider(api_key=api_key)

    elif provider == "azure-openai":
        raise NotImplementedError("Azure provider coming soon")

    elif provider == "local":
        raise NotImplementedError("Local provider coming soon")

    else:
        raise ValueError(f"Unknown provider: {provider!r}")


# ---------------------------------------------------------------------------
# ConverseExecutor
# ---------------------------------------------------------------------------

class ConverseExecutor:
    """Multi-turn executor using Bedrock Converse API with tool use.

    Parameters
    ----------
    profile:
        AWS named profile. None uses ambient credential chain.
    region:
        AWS region for Bedrock Runtime endpoint.
    budget_limit_usd:
        Daily budget limit. Execution is refused when exceeded.
    allowed_tools:
        List of tool names the agent is allowed to use.
        None means all tools are available.
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        region: str = "eu-central-1",
        budget_limit_usd: float = 5.0,
        allowed_tools: Optional[list] = None,
        provider_client: Optional[Any] = None,
    ) -> None:
        self._profile = profile
        self._region = region
        self._budget_limit_usd = budget_limit_usd
        self._allowed_tools = allowed_tools
        self._client = None
        self._provider_client = provider_client  # AnthropicProvider or None
        self._available: Optional[bool] = None

    # ------------------------------------------------------------------
    # Client initialization
    # ------------------------------------------------------------------

    def _get_client(self):
        """Get the appropriate client based on provider configuration.

        Returns the provider_client if set (for anthropic-api), otherwise
        returns the boto3 bedrock-runtime client (for aws-bedrock).
        """
        if self._provider_client is not None:
            return self._provider_client
        return self._client

    def _ensure_client(self) -> None:
        """Create the boto3 Bedrock Runtime client on first use.

        If a provider_client (e.g. AnthropicProvider) was passed at init,
        we skip boto3 client creation entirely.
        """
        if self._provider_client is not None:
            # Provider client already available — no boto3 needed
            self._available = True
            return

        if self._client is not None or self._available is False:
            return

        session_kwargs: dict = {"region_name": self._region}
        if self._profile:
            session_kwargs["profile_name"] = self._profile

        try:
            session = boto3.Session(**session_kwargs)
            self._client = session.client("bedrock-runtime")
            logger.debug(
                "ConverseExecutor: client initialised (region=%s, profile=%s)",
                self._region,
                self._profile or "<ambient>",
            )
        except (NoCredentialsError, NoRegionError) as exc:
            logger.warning("ConverseExecutor: credentials unavailable: %s", exc)
            self._available = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("ConverseExecutor: client init failed: %s", exc)
            self._available = False

    @property
    def is_available(self) -> Optional[bool]:
        """Availability state."""
        return self._available

    # ------------------------------------------------------------------
    # Budget check
    # ------------------------------------------------------------------

    def _check_budget(self) -> Optional[str]:
        """Return error string if budget is exceeded, else None."""
        try:
            from cap.harness.cost_meter import budget_remaining
            remaining = budget_remaining(daily_limit_usd=self._budget_limit_usd)
            if remaining <= 0:
                return f"daily budget exceeded (limit=${self._budget_limit_usd})"
        except Exception:  # noqa: BLE001
            pass  # If cost meter unavailable, don't block
        return None

    # ------------------------------------------------------------------
    # Tool config builder
    # ------------------------------------------------------------------

    def _get_tool_config(self) -> Optional[dict]:
        """Build the toolConfig for the Converse API call."""
        tools = []
        for tool_def in TOOL_DEFINITIONS:
            tool_name = tool_def["toolSpec"]["name"]
            if self._allowed_tools is None or tool_name in self._allowed_tools:
                tools.append(tool_def)

        if not tools:
            return None

        return {"tools": tools}

    # ------------------------------------------------------------------
    # Single API call with retry
    # ------------------------------------------------------------------

    def _call_converse(
        self,
        model_id: str,
        messages: list,
        system: Optional[list] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        tool_config: Optional[dict] = None,
    ) -> dict:
        """Make a single Converse API call with exponential backoff retry.

        Routes to the appropriate provider:
        - If provider_client is set (AnthropicProvider), uses its converse() method
        - Otherwise, uses the boto3 bedrock-runtime client

        Returns the raw API response dict (in Bedrock Converse format regardless of provider).
        Raises on unrecoverable errors.
        """
        # Use provider client if available (e.g. AnthropicProvider)
        if self._provider_client is not None:
            return self._provider_client.converse(
                model_id=model_id,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_config=tool_config,
            )

        # Default: boto3 Bedrock Converse API
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            kwargs["system"] = system
        if tool_config:
            kwargs["toolConfig"] = tool_config

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._client.converse(**kwargs)
                return response
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "ThrottlingException" and attempt < MAX_RETRIES:
                    delay = BACKOFF_BASE_S * (BACKOFF_MULTIPLIER ** attempt)
                    logger.warning(
                        "ConverseExecutor: throttled, retry %d/%d in %.1fs",
                        attempt + 1, MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                    last_error = exc
                    continue
                raise

        # Should not reach here, but just in case
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------

    def execute(
        self,
        agent_id: str,
        agent_type: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        context: Optional[str] = None,
    ) -> ConversationResult:
        """Execute a multi-turn conversation with tool use.

        Parameters
        ----------
        agent_id:
            UUID of the agent record.
        agent_type:
            Agent role (dev, security, etc.) — used for system prompt loading.
        prompt:
            The user task prompt.
        system_prompt:
            Override system prompt. If None, loaded from agent definition.
        model:
            Model shortname or full ID. Defaults to agent_type's default.
        max_tokens:
            Max tokens per turn.
        temperature:
            Sampling temperature.
        context:
            Optional context to prepend to the prompt (from hooks_pre_task).

        Returns
        -------
        ConversationResult with the final response or error.
        """
        self._ensure_client()

        resolved_model = _resolve_model(model)
        start_ts = time.monotonic()

        def _error_result(error: str, turns: int = 0) -> ConversationResult:
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=error,
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_usd=0.0,
                duration_ms=elapsed,
                turns=turns,
            )

        # Check availability
        if self._available is False or (self._client is None and self._provider_client is None):
            return _error_result("bedrock unavailable: client not initialised")

        # Check budget
        budget_error = self._check_budget()
        if budget_error:
            return _error_result(budget_error)

        # Load system prompt from agent definition if not provided
        if system_prompt is None:
            system_prompt = load_agent_system_prompt(agent_type)

        # Build system message (Converse API format)
        system_messages = None
        if system_prompt:
            system_messages = [{"text": system_prompt}]

        # Build initial user message
        user_content = prompt
        if context:
            user_content = f"## Context\n{context}\n\n## Task\n{prompt}"

        messages: list[dict] = [
            {"role": "user", "content": [{"text": user_content}]}
        ]

        # Tool config
        tool_config = self._get_tool_config()

        # Execution loop
        total_input_tokens = 0
        total_output_tokens = 0
        tool_call_log: list[dict] = []
        turns = 0

        try:
            for iteration in range(MAX_TOOL_ITERATIONS):
                turns += 1

                # Budget check each turn (skip first — already checked above)
                if iteration > 0:
                    budget_error = self._check_budget()
                    if budget_error:
                        elapsed = int((time.monotonic() - start_ts) * 1000)
                        return ConversationResult(
                            agent_id=agent_id,
                            agent_type=agent_type,
                            model=resolved_model,
                            response=None,
                            error=f"budget exceeded mid-conversation at turn {turns}",
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                            total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                            duration_ms=elapsed,
                            turns=turns,
                            tool_calls=tool_call_log,
                        )

                # Call Converse API
                response = self._call_converse(
                    model_id=resolved_model,
                    messages=messages,
                    system=system_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tool_config=tool_config,
                )

                # Extract token usage
                usage = response.get("usage", {})
                total_input_tokens += usage.get("inputTokens", 0)
                total_output_tokens += usage.get("outputTokens", 0)

                # Check stop reason
                stop_reason = response.get("stopReason", "end_turn")
                output = response.get("output", {})
                message = output.get("message", {})
                content_blocks = message.get("content", [])

                # If stop reason is "end_turn" or "max_tokens" — extract final text
                if stop_reason in ("end_turn", "max_tokens"):
                    text_parts = []
                    for block in content_blocks:
                        if "text" in block:
                            text_parts.append(block["text"])

                    final_text = "\n".join(text_parts) if text_parts else ""

                    self._available = True
                    elapsed = int((time.monotonic() - start_ts) * 1000)
                    return ConversationResult(
                        agent_id=agent_id,
                        agent_type=agent_type,
                        model=resolved_model,
                        response=final_text,
                        error=None,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                        total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                        duration_ms=elapsed,
                        turns=turns,
                        tool_calls=tool_call_log,
                    )

                # If stop reason is "tool_use" — process tool calls
                if stop_reason == "tool_use":
                    # Add assistant message to conversation
                    messages.append({"role": "assistant", "content": content_blocks})

                    # Process each tool_use block
                    tool_results = []
                    for block in content_blocks:
                        if "toolUse" in block:
                            tool_use = block["toolUse"]
                            tool_name = tool_use["name"]
                            tool_input = tool_use.get("input", {})
                            tool_use_id = tool_use["toolUseId"]

                            # Execute the tool
                            tool_output = execute_tool(tool_name, tool_input)

                            tool_call_log.append({
                                "tool": tool_name,
                                "input": tool_input,
                                "output_preview": tool_output[:200],
                                "iteration": iteration,
                            })

                            tool_results.append({
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"text": tool_output}],
                                }
                            })

                    # Add tool results as user message
                    messages.append({"role": "user", "content": tool_results})
                    continue

                # Unknown stop reason — treat as completion
                text_parts = []
                for block in content_blocks:
                    if "text" in block:
                        text_parts.append(block["text"])

                self._available = True
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    model=resolved_model,
                    response="\n".join(text_parts),
                    error=None,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                    duration_ms=int((time.monotonic() - start_ts) * 1000),
                    turns=turns,
                    tool_calls=tool_call_log,
                )

            # Hit max iterations
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=f"max tool iterations reached ({MAX_TOOL_ITERATIONS})",
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed,
                turns=turns,
                tool_calls=tool_call_log,
            )

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"].get("Message", str(exc))

            if code == "ThrottlingException":
                error_str = "throttled (retries exhausted)"
            elif code == "ValidationException":
                error_str = f"validation: {msg}"
            elif code == "ModelNotReadyException":
                error_str = "model_not_ready"
            else:
                error_str = f"{code}: {msg}"
                self._available = False

            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=error_str,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed,
                turns=turns,
                tool_calls=tool_call_log,
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("ConverseExecutor: unexpected error: %s", exc, exc_info=True)
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=str(exc),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed,
                turns=turns,
                tool_calls=tool_call_log,
            )

    # ------------------------------------------------------------------
    # Streaming execution
    # ------------------------------------------------------------------

    def execute_streaming(
        self,
        agent_id: str,
        agent_type: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
    ) -> ConversationResult:
        """Execute with streaming response (single turn, no tool use).

        Uses converse_stream() for real-time output on long-running tasks.
        Does NOT support tool use (streaming + tool use is complex).
        For tool-use tasks, use execute() instead.
        """
        self._ensure_client()

        resolved_model = _resolve_model(model)
        start_ts = time.monotonic()

        if self._available is False or (self._client is None and self._provider_client is None):
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error="bedrock unavailable",
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_usd=0.0,
                duration_ms=0,
                turns=0,
            )

        if system_prompt is None:
            system_prompt = load_agent_system_prompt(agent_type)

        system_messages = [{"text": system_prompt}] if system_prompt else None
        messages: list[dict] = [{"role": "user", "content": [{"text": prompt}]}]

        # Use provider client for streaming if available
        if self._provider_client is not None:
            try:
                response = self._provider_client.converse_stream(
                    model_id=resolved_model,
                    messages=messages,
                    system=system_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # Process streamed events (same format as boto3 stream)
                text_parts = []
                input_tokens = 0
                output_tokens = 0
                stream = response.get("stream", [])
                for event in stream:
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        if "text" in delta:
                            text_parts.append(delta["text"])
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        input_tokens = usage.get("inputTokens", 0)
                        output_tokens = usage.get("outputTokens", 0)

                final_text = "".join(text_parts)
                self._available = True
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    model=resolved_model,
                    response=final_text,
                    error=None,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                    duration_ms=elapsed,
                    turns=1,
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    model=resolved_model,
                    response=None,
                    error=str(exc),
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cost_usd=0.0,
                    duration_ms=elapsed,
                    turns=1,
                )

        kwargs: dict[str, Any] = {
            "modelId": resolved_model,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_messages:
            kwargs["system"] = system_messages

        try:
            response = self._client.converse_stream(**kwargs)

            # Collect streamed content
            text_parts = []
            input_tokens = 0
            output_tokens = 0

            stream = response.get("stream", [])
            for event in stream:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        text_parts.append(delta["text"])
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = usage.get("inputTokens", 0)
                    output_tokens = usage.get("outputTokens", 0)

            final_text = "".join(text_parts)
            self._available = True
            elapsed = int((time.monotonic() - start_ts) * 1000)

            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=final_text,
                error=None,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                duration_ms=elapsed,
                turns=1,
            )

        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=f"{code}: {exc.response['Error'].get('Message', '')}",
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_usd=0.0,
                duration_ms=elapsed,
                turns=1,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=str(exc),
                total_input_tokens=0,
                total_output_tokens=0,
                total_cost_usd=0.0,
                duration_ms=elapsed,
                turns=1,
            )

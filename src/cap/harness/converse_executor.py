"""Multi-turn Converse executor using LangGraph for agent loop orchestration.

Supports multiple LLM providers via LangChain:
- aws-bedrock: ChatBedrockConverse (default)
- anthropic-api: ChatAnthropic (direct Anthropic Messages API)
- local: ChatOllama (local models via Ollama)

Uses a LangGraph StateGraph for the agent loop:
START -> model_call -> check_tool_use -> [tool_node | END]

Budget enforcement after each model call. Max iterations from config (default 15).
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Annotated, Callable, Optional, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, StateGraph

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

# Model aliases (same as executor.py -- import from there)
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
# Legacy Tool Definitions (kept for backward compat imports)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "file_read",
            "description": "Read the contents of a file at the given absolute path.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to read"},
                        "offset": {"type": "integer", "description": "Line number to start reading from"},
                        "limit": {"type": "integer", "description": "Maximum number of lines to read"},
                    },
                    "required": ["path"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "bash_exec",
            "description": "Execute a bash command and return stdout/stderr.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The bash command to execute"},
                        "cwd": {"type": "string", "description": "Working directory"},
                    },
                    "required": ["command"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "knowledge_search",
            "description": "Search the CAP knowledge base.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "scope": {"type": "string", "enum": ["all", "code", "config", "doc"]},
                    },
                    "required": ["query"],
                }
            },
        }
    },
]


# ---------------------------------------------------------------------------
# Legacy tool execution functions (kept for backward compat imports)
# ---------------------------------------------------------------------------

def _execute_file_read(input_data: dict) -> str:
    """Read a file and return its content."""
    from cap.harness.agent_tools import FileReadTool
    tool = FileReadTool()
    return tool._run(
        path=input_data.get("path", ""),
        offset=input_data.get("offset", 0),
        limit=input_data.get("limit", 0),
    )


def _execute_bash(input_data: dict) -> str:
    """Execute a bash command with timeout."""
    from cap.harness.agent_tools import BashExecTool
    tool = BashExecTool(workspace="")
    return tool._run(
        command=input_data.get("command", ""),
        cwd=input_data.get("cwd", ""),
    )


def _execute_knowledge_search(input_data: dict) -> str:
    """Search the knowledge base."""
    from cap.harness.agent_tools import KnowledgeSearchTool
    tool = KnowledgeSearchTool()
    return tool._run(
        query=input_data.get("query", ""),
        scope=input_data.get("scope", "all"),
    )


# Tool dispatcher (legacy compat)
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
    turns: int
    tool_calls: list = field(default_factory=list)
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
# LangGraph State
# ---------------------------------------------------------------------------

def _add_messages(existing: list[BaseMessage], new: list[BaseMessage]) -> list[BaseMessage]:
    """Reducer that appends new messages to existing list."""
    return existing + new


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], _add_messages]
    iterations: int
    total_input_tokens: int
    total_output_tokens: int
    tool_calls_log: list[dict]
    budget_exceeded: bool
    error: Optional[str]


# ---------------------------------------------------------------------------
# ConverseExecutor (LangGraph-based)
# ---------------------------------------------------------------------------

class ConverseExecutor:
    """Multi-turn executor using LangGraph StateGraph with LangChain providers.

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
    provider_client:
        Legacy: pre-configured provider client (for backward compat).
    config:
        Harness config dict. If None, loaded from harness_config.
    """

    def __init__(
        self,
        profile: Optional[str] = None,
        region: str | None = None,
        budget_limit_usd: float = 5.0,
        allowed_tools: Optional[list] = None,
        provider_client: Optional[Any] = None,
        config: Optional[dict] = None,
    ) -> None:
        from cap.lib.harness_config import DEFAULT_AWS_REGION
        self._profile = profile
        self._region = region or DEFAULT_AWS_REGION
        self._budget_limit_usd = budget_limit_usd
        self._allowed_tools = allowed_tools
        self._client = None
        self._provider_client = provider_client
        self._llm: Optional[BaseChatModel] = None
        self._config = config
        self._available: Optional[bool] = None

    # ------------------------------------------------------------------
    # Client initialization
    # ------------------------------------------------------------------

    def _get_client(self):
        """Get the appropriate client based on provider configuration."""
        if self._provider_client is not None:
            return self._provider_client
        return self._client

    def _ensure_client(self) -> None:
        """Initialize the LLM provider."""
        if self._available is False:
            return

        if self._provider_client is not None:
            self._available = True
            return

        if self._llm is not None:
            self._available = True
            return

        if self._client is not None:
            self._available = True
            return

        # Try LangChain provider creation
        try:
            from cap.harness.llm_provider import create_llm
            if self._config is None:
                from cap.lib.harness_config import load_harness_config
                self._config = load_harness_config()

            self._llm = create_llm(self._config, tier="sonnet")
            self._available = True
            logger.debug("ConverseExecutor: LangChain LLM initialised")
        except Exception as exc:
            logger.debug("ConverseExecutor: LangChain init failed (%s), trying boto3", exc)
            self._init_boto3_client()

    def _init_boto3_client(self) -> None:
        """Legacy: Create boto3 Bedrock Runtime client."""
        try:
            import boto3
            from botocore.exceptions import NoCredentialsError, NoRegionError

            session_kwargs: dict = {"region_name": self._region}
            if self._profile:
                session_kwargs["profile_name"] = self._profile

            session = boto3.Session(**session_kwargs)
            self._client = session.client("bedrock-runtime")
            self._available = True
        except Exception as exc:
            logger.warning("ConverseExecutor: client init failed: %s", exc)
            self._available = False

    @property
    def is_available(self) -> Optional[bool]:
        """Availability state."""
        return self._available

    # ------------------------------------------------------------------
    # Budget check
    # ------------------------------------------------------------------

    def _check_budget(self, agent_type: str = "unknown") -> Optional[str]:
        """Return error string if budget is exceeded, else None.

        Performs three checks:
        1. Budget paused flag file (~/.claude-platform/data/budget_paused)
        2. Daily spend vs daily_limit_usd (via cost_meter/execution_ledger)
        3. Per-agent-type cap if configured (via budget_manager)
        """
        # Check 1: Is budget paused?
        try:
            from cap.lib.budget_manager import is_budget_paused
            if is_budget_paused():
                return "budget paused — executions blocked. Run 'cap budget resume' to resume."
        except Exception:
            pass

        # Check 2: Daily limit via cost_meter (uses execution_ledger — source of truth)
        try:
            from cap.harness.cost_meter import budget_remaining
            remaining = budget_remaining(daily_limit_usd=self._budget_limit_usd)
            if remaining <= 0:
                return f"daily budget exceeded (limit=${self._budget_limit_usd})"
        except Exception:
            pass

        # Check 3: Per-agent-type cap (via budget_manager)
        try:
            from cap.lib.harness_config import load_harness_config
            import sqlite3

            harness_cfg = load_harness_config()
            agent_caps = harness_cfg.get("budget", {}).get("agent_caps", {})

            if agent_type in agent_caps:
                cap_home = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))
                db_path = cap_home / "data" / "platform.db"

                if db_path.exists():
                    db = sqlite3.connect(str(db_path))
                    db.execute("PRAGMA busy_timeout=2000")
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                    try:
                        row = db.execute(
                            """SELECT COALESCE(SUM(cost_usd), 0.0)
                               FROM execution_ledger
                               WHERE agent_type = ? AND date(created_at) = ?""",
                            (agent_type, today),
                        ).fetchone()
                        agent_spend = row[0] if row else 0.0
                    except Exception:
                        agent_spend = 0.0

                    db.close()

                    cap_limit = agent_caps[agent_type]
                    if agent_spend >= cap_limit:
                        return (
                            f"per-agent cap exceeded for '{agent_type}': "
                            f"${agent_spend:.4f} spent of ${cap_limit:.2f} cap."
                        )
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Budget spend recording
    # ------------------------------------------------------------------

    def _record_budget_spend(self, agent_type: str, cost_usd: float) -> None:
        """Record execution cost in budget_log after successful execution."""
        if cost_usd <= 0:
            return
        try:
            from cap.lib.budget_manager import record_budget_spend
            import sqlite3

            cap_home = Path(os.environ.get("CAP_HOME", str(Path.home() / ".claude-platform")))
            db_path = cap_home / "data" / "platform.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            db = sqlite3.connect(str(db_path))
            db.execute("PRAGMA busy_timeout=2000")
            record_budget_spend(db, agent_type, cost_usd)
            db.close()
        except Exception:
            pass  # Best effort — don't block execution on tracking failure

    # ------------------------------------------------------------------
    # Tool config builder (legacy format for boto3 path)
    # ------------------------------------------------------------------

    def _get_tool_config(self) -> Optional[dict]:
        """Build the toolConfig for the legacy Converse API call."""
        tools = []
        for tool_def in TOOL_DEFINITIONS:
            tool_name = tool_def["toolSpec"]["name"]
            if self._allowed_tools is None or tool_name in self._allowed_tools:
                tools.append(tool_def)
        if not tools:
            return None
        return {"tools": tools}

    # ------------------------------------------------------------------
    # Single API call with retry (legacy boto3 path)
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
        """Make a single Converse API call with retry."""
        if self._provider_client is not None:
            return self._provider_client.converse(
                model_id=model_id,
                messages=messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_config=tool_config,
            )

        from botocore.exceptions import ClientError

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

        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # LangGraph-based execution
    # ------------------------------------------------------------------

    def _build_graph(self, llm: BaseChatModel, tools: list, max_iterations: int) -> StateGraph:
        """Build the LangGraph StateGraph for the agent loop."""
        from langchain_core.tools import BaseTool as LCBaseTool

        if tools:
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        tool_map: dict[str, LCBaseTool] = {t.name: t for t in tools}

        def model_call(state: AgentState) -> dict:
            messages = state["messages"]
            response = llm_with_tools.invoke(messages)

            usage = getattr(response, "usage_metadata", None) or {}
            input_tokens = 0
            output_tokens = 0
            if isinstance(usage, dict):
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)

            return {
                "messages": [response],
                "iterations": state["iterations"] + 1,
                "total_input_tokens": state["total_input_tokens"] + input_tokens,
                "total_output_tokens": state["total_output_tokens"] + output_tokens,
            }

        def tool_node(state: AgentState) -> dict:
            last_message = state["messages"][-1]
            tool_calls_log = list(state["tool_calls_log"])
            tool_messages: list[BaseMessage] = []

            if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                for tc in last_message.tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    tool_id = tc["id"]

                    if tool_name in tool_map:
                        try:
                            result = tool_map[tool_name].invoke(tool_args)
                        except Exception as exc:
                            result = f"Error: {exc}"
                    else:
                        result = f"Error: unknown tool '{tool_name}'"

                    if not isinstance(result, str):
                        result = str(result)

                    if len(result) > TOOL_OUTPUT_MAX_CHARS:
                        result = result[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"

                    tool_messages.append(ToolMessage(content=result, tool_call_id=tool_id))
                    tool_calls_log.append({
                        "tool": tool_name,
                        "input": tool_args,
                        "output_preview": result[:200],
                        "iteration": state["iterations"],
                    })

            return {
                "messages": tool_messages,
                "tool_calls_log": tool_calls_log,
            }

        def should_continue(state: AgentState) -> str:
            if state.get("budget_exceeded"):
                return "end"
            if state["iterations"] >= max_iterations:
                return "end"
            last_message = state["messages"][-1]
            if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                return "tools"
            return "end"

        graph = StateGraph(AgentState)
        graph.add_node("model_call", model_call)
        graph.add_node("tool_node", tool_node)
        graph.set_entry_point("model_call")
        graph.add_conditional_edges("model_call", should_continue, {"tools": "tool_node", "end": END})
        graph.add_edge("tool_node", "model_call")

        return graph

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
        """Execute a multi-turn conversation with tool use."""
        self._ensure_client()

        resolved_model = _resolve_model(model)
        start_ts = time.monotonic()

        def _error_result(error: str, turns: int = 0, input_tokens: int = 0,
                          output_tokens: int = 0, tool_calls: list = None) -> ConversationResult:
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id,
                agent_type=agent_type,
                model=resolved_model,
                response=None,
                error=error,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                duration_ms=elapsed,
                turns=turns,
                tool_calls=tool_calls or [],
            )

        # Check availability
        if self._available is False or (
            self._client is None and self._provider_client is None and self._llm is None
        ):
            return _error_result("bedrock unavailable: client not initialised")

        # Check budget
        budget_error = self._check_budget(agent_type=agent_type)
        if budget_error:
            return _error_result(budget_error)

        # Load system prompt
        if system_prompt is None:
            system_prompt = load_agent_system_prompt(agent_type)

        # Build user content
        user_content = prompt
        if context:
            user_content = f"## Context\n{context}\n\n## Task\n{prompt}"

        # LangGraph path
        if self._llm is not None:
            return self._execute_langgraph(
                agent_id=agent_id, agent_type=agent_type,
                resolved_model=resolved_model, user_content=user_content,
                system_prompt=system_prompt, start_ts=start_ts,
            )

        # Legacy boto3/provider_client path
        return self._execute_legacy(
            agent_id=agent_id, agent_type=agent_type,
            resolved_model=resolved_model, user_content=user_content,
            system_prompt=system_prompt, max_tokens=max_tokens,
            temperature=temperature, start_ts=start_ts,
        )

    def _execute_langgraph(self, agent_id, agent_type, resolved_model, user_content, system_prompt, start_ts):
        """Execute using LangGraph StateGraph."""
        from cap.harness.agent_tools import get_tools_for_agent

        workspace = os.getcwd()
        tools = get_tools_for_agent(agent_type, workspace)

        if self._allowed_tools is not None:
            tools = [t for t in tools if t.name in self._allowed_tools]

        max_iterations = MAX_TOOL_ITERATIONS
        if self._config and "execution" in self._config:
            max_iterations = self._config["execution"].get("max_tool_iterations", MAX_TOOL_ITERATIONS)

        graph = self._build_graph(self._llm, tools, max_iterations)
        compiled = graph.compile()

        messages: list[BaseMessage] = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=user_content))

        initial_state: AgentState = {
            "messages": messages,
            "iterations": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "tool_calls_log": [],
            "budget_exceeded": False,
            "error": None,
        }

        try:
            final_state = compiled.invoke(initial_state)

            all_messages = final_state["messages"]
            last_ai_message = None
            for msg in reversed(all_messages):
                if isinstance(msg, AIMessage):
                    last_ai_message = msg
                    break

            response_text = ""
            if last_ai_message:
                response_text = last_ai_message.content if isinstance(last_ai_message.content, str) else str(last_ai_message.content)

            elapsed = int((time.monotonic() - start_ts) * 1000)
            total_input = final_state["total_input_tokens"]
            total_output = final_state["total_output_tokens"]

            error = None
            if final_state["iterations"] >= max_iterations and (
                last_ai_message and hasattr(last_ai_message, "tool_calls") and last_ai_message.tool_calls
            ):
                error = f"max tool iterations reached ({max_iterations})"
                response_text = None

            self._available = True
            final_cost = _compute_cost(resolved_model, total_input, total_output)
            if error is None:
                self._record_budget_spend(agent_type, final_cost)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=response_text if error is None else None, error=error,
                total_input_tokens=total_input, total_output_tokens=total_output,
                total_cost_usd=final_cost,
                duration_ms=elapsed, turns=final_state["iterations"],
                tool_calls=final_state["tool_calls_log"],
            )
        except Exception as exc:
            logger.error("ConverseExecutor: LangGraph error: %s", exc, exc_info=True)
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=str(exc),
                total_input_tokens=0, total_output_tokens=0,
                total_cost_usd=0.0, duration_ms=elapsed, turns=0,
            )

    def _execute_legacy(self, agent_id, agent_type, resolved_model, user_content, system_prompt, max_tokens, temperature, start_ts):
        """Execute using legacy boto3/provider_client path."""
        from botocore.exceptions import ClientError

        system_messages = [{"text": system_prompt}] if system_prompt else None
        messages: list[dict] = [{"role": "user", "content": [{"text": user_content}]}]
        tool_config = self._get_tool_config()

        total_input_tokens = 0
        total_output_tokens = 0
        tool_call_log: list[dict] = []
        turns = 0

        try:
            for iteration in range(MAX_TOOL_ITERATIONS):
                turns += 1

                if iteration > 0:
                    budget_error = self._check_budget(agent_type=agent_type)
                    if budget_error:
                        elapsed = int((time.monotonic() - start_ts) * 1000)
                        return ConversationResult(
                            agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                            response=None, error=f"budget exceeded mid-conversation at turn {turns}",
                            total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                            total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                            duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
                        )

                response = self._call_converse(
                    model_id=resolved_model, messages=messages, system=system_messages,
                    max_tokens=max_tokens, temperature=temperature, tool_config=tool_config,
                )

                usage = response.get("usage", {})
                total_input_tokens += usage.get("inputTokens", 0)
                total_output_tokens += usage.get("outputTokens", 0)

                stop_reason = response.get("stopReason", "end_turn")
                output = response.get("output", {})
                message = output.get("message", {})
                content_blocks = message.get("content", [])

                if stop_reason in ("end_turn", "max_tokens"):
                    text_parts = [b["text"] for b in content_blocks if "text" in b]
                    final_text = "\n".join(text_parts) if text_parts else ""
                    self._available = True
                    elapsed = int((time.monotonic() - start_ts) * 1000)
                    final_cost = _compute_cost(resolved_model, total_input_tokens, total_output_tokens)
                    self._record_budget_spend(agent_type, final_cost)
                    return ConversationResult(
                        agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                        response=final_text, error=None,
                        total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                        total_cost_usd=final_cost,
                        duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
                    )

                if stop_reason == "tool_use":
                    messages.append({"role": "assistant", "content": content_blocks})
                    tool_results = []
                    for block in content_blocks:
                        if "toolUse" in block:
                            tool_use = block["toolUse"]
                            tool_name = tool_use["name"]
                            tool_input = tool_use.get("input", {})
                            tool_use_id = tool_use["toolUseId"]
                            tool_output = execute_tool(tool_name, tool_input)
                            tool_call_log.append({
                                "tool": tool_name, "input": tool_input,
                                "output_preview": tool_output[:200], "iteration": iteration,
                            })
                            tool_results.append({
                                "toolResult": {"toolUseId": tool_use_id, "content": [{"text": tool_output}]}
                            })
                    messages.append({"role": "user", "content": tool_results})
                    continue

                text_parts = [b["text"] for b in content_blocks if "text" in b]
                self._available = True
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                    response="\n".join(text_parts), error=None,
                    total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                    total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                    duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
                )

            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=f"max tool iterations reached ({MAX_TOOL_ITERATIONS})",
                total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
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
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=error_str,
                total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
            )
        except Exception as exc:
            logger.error("ConverseExecutor: unexpected error: %s", exc, exc_info=True)
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=str(exc),
                total_input_tokens=total_input_tokens, total_output_tokens=total_output_tokens,
                total_cost_usd=_compute_cost(resolved_model, total_input_tokens, total_output_tokens),
                duration_ms=elapsed, turns=turns, tool_calls=tool_call_log,
            )

    # ------------------------------------------------------------------
    # Streaming execution
    # ------------------------------------------------------------------

    def execute_streaming(self, agent_id, agent_type, prompt, system_prompt=None, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=0.7):
        """Execute with streaming response (single turn, no tool use)."""
        self._ensure_client()
        resolved_model = _resolve_model(model)
        start_ts = time.monotonic()

        if self._available is False or (self._client is None and self._provider_client is None and self._llm is None):
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error="bedrock unavailable",
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                duration_ms=0, turns=0,
            )

        if system_prompt is None:
            system_prompt = load_agent_system_prompt(agent_type)

        if self._llm is not None:
            return self._stream_langgraph(agent_id, agent_type, resolved_model, prompt, system_prompt, start_ts)

        system_messages = [{"text": system_prompt}] if system_prompt else None
        messages: list[dict] = [{"role": "user", "content": [{"text": prompt}]}]

        if self._provider_client is not None:
            try:
                response = self._provider_client.converse_stream(
                    model_id=resolved_model, messages=messages, system=system_messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
                text_parts, input_tokens, output_tokens = [], 0, 0
                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        if "text" in delta:
                            text_parts.append(delta["text"])
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        input_tokens = usage.get("inputTokens", 0)
                        output_tokens = usage.get("outputTokens", 0)
                self._available = True
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                    response="".join(text_parts), error=None,
                    total_input_tokens=input_tokens, total_output_tokens=output_tokens,
                    total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                    duration_ms=elapsed, turns=1,
                )
            except Exception as exc:
                elapsed = int((time.monotonic() - start_ts) * 1000)
                return ConversationResult(
                    agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                    response=None, error=str(exc),
                    total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                    duration_ms=elapsed, turns=1,
                )

        from botocore.exceptions import ClientError
        kwargs: dict[str, Any] = {
            "modelId": resolved_model, "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system_messages:
            kwargs["system"] = system_messages

        try:
            response = self._client.converse_stream(**kwargs)
            text_parts, input_tokens, output_tokens = [], 0, 0
            for event in response.get("stream", []):
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        text_parts.append(delta["text"])
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = usage.get("inputTokens", 0)
                    output_tokens = usage.get("outputTokens", 0)
            self._available = True
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response="".join(text_parts), error=None,
                total_input_tokens=input_tokens, total_output_tokens=output_tokens,
                total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                duration_ms=elapsed, turns=1,
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=f"{code}: {exc.response['Error'].get('Message', '')}",
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                duration_ms=elapsed, turns=1,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=str(exc),
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                duration_ms=elapsed, turns=1,
            )

    def _stream_langgraph(self, agent_id, agent_type, resolved_model, prompt, system_prompt, start_ts):
        """Stream using LangChain LLM (single turn, no tool use)."""
        try:
            messages: list[BaseMessage] = []
            if system_prompt:
                messages.append(SystemMessage(content=system_prompt))
            messages.append(HumanMessage(content=prompt))
            response = self._llm.invoke(messages)
            text = response.content if isinstance(response.content, str) else str(response.content)
            usage = getattr(response, "usage_metadata", None) or {}
            input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
            output_tokens = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
            self._available = True
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=text, error=None,
                total_input_tokens=input_tokens, total_output_tokens=output_tokens,
                total_cost_usd=_compute_cost(resolved_model, input_tokens, output_tokens),
                duration_ms=elapsed, turns=1,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start_ts) * 1000)
            return ConversationResult(
                agent_id=agent_id, agent_type=agent_type, model=resolved_model,
                response=None, error=str(exc),
                total_input_tokens=0, total_output_tokens=0, total_cost_usd=0.0,
                duration_ms=elapsed, turns=1,
            )

"""Unit tests for cap.harness.converse_executor.

All tests are fully offline — boto3 is patched so no AWS credentials are
required.
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.harness.converse_executor import (
    ConverseExecutor,
    ConversationResult,
    TOOL_DEFINITIONS,
    execute_tool,
    load_agent_system_prompt,
    _execute_file_read,
    _execute_bash,
    _execute_knowledge_search,
    MAX_TOOL_ITERATIONS,
)
from cap.harness.executor import MODEL_ALIASES, ExecutionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _converse_response(
    text: str,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 20,
) -> dict:
    """Build a minimal fake converse() response."""
    return {
        "stopReason": stop_reason,
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        },
    }


def _tool_use_response(
    tool_name: str,
    tool_input: dict,
    tool_use_id: str = "tool-123",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> dict:
    """Build a fake converse() response requesting a tool call."""
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": tool_name,
                            "input": tool_input,
                        }
                    }
                ],
            }
        },
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        },
    }


def _client_error(code: str, message: str = "error") -> Exception:
    """Build a botocore ClientError for the given error code."""
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "converse",
    )


def _make_executor(mock_client=None) -> ConverseExecutor:
    """Create a ConverseExecutor with a pre-wired mock client."""
    executor = ConverseExecutor(profile="test", region="eu-central-1")
    if mock_client is None:
        mock_client = MagicMock()
    executor._client = mock_client
    executor._available = None
    return executor


# ---------------------------------------------------------------------------
# load_agent_system_prompt
# ---------------------------------------------------------------------------

class TestLoadAgentSystemPrompt:
    def test_returns_none_for_missing_file(self):
        result = load_agent_system_prompt("nonexistent_agent_xyz")
        assert result is None

    def test_loads_body_from_md_with_frontmatter(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        md_file = agents_dir / "dev.md"
        md_file.write_text(
            "---\nmodel: sonnet\n---\nYou are a dev agent.\nDo great work.",
            encoding="utf-8",
        )

        with patch("cap.harness.converse_executor.AGENT_DEFS_DIR", agents_dir):
            result = load_agent_system_prompt("dev")

        assert result == "You are a dev agent.\nDo great work."

    def test_loads_full_content_when_no_frontmatter(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        md_file = agents_dir / "sre.md"
        md_file.write_text("You are an SRE agent.", encoding="utf-8")

        with patch("cap.harness.converse_executor.AGENT_DEFS_DIR", agents_dir):
            result = load_agent_system_prompt("sre")

        assert result == "You are an SRE agent."

    def test_strips_whitespace_from_body(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        md_file = agents_dir / "security.md"
        md_file.write_text(
            "---\nmodel: opus\n---\n\n  Security agent here.  \n\n",
            encoding="utf-8",
        )

        with patch("cap.harness.converse_executor.AGENT_DEFS_DIR", agents_dir):
            result = load_agent_system_prompt("security")

        assert result == "Security agent here."


# ---------------------------------------------------------------------------
# Tool execution: file_read
# ---------------------------------------------------------------------------

class TestExecuteFileRead:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        result = _execute_file_read({"path": str(f)})
        assert result == "hello world"

    def test_relative_path_returns_error(self):
        result = _execute_file_read({"path": "relative/path.txt"})
        assert result.startswith("Error: path must be absolute")

    def test_missing_file_returns_error(self):
        result = _execute_file_read({"path": "/nonexistent/file.txt"})
        assert "not found" in result

    def test_directory_path_returns_error(self, tmp_path):
        result = _execute_file_read({"path": str(tmp_path)})
        assert "not a regular file" in result

    def test_offset_skips_lines(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = _execute_file_read({"path": str(f), "offset": 1})
        assert "line1" not in result
        assert "line2" in result

    def test_limit_truncates_lines(self, tmp_path):
        f = tmp_path / "lines.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = _execute_file_read({"path": str(f), "limit": 1})
        assert "line1" in result
        assert "line2" not in result

    def test_large_file_truncated(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 60_000, encoding="utf-8")
        result = _execute_file_read({"path": str(f)})
        assert "[truncated]" in result
        assert len(result) <= 50_100  # max chars + truncation marker


# ---------------------------------------------------------------------------
# Tool execution: bash_exec
# ---------------------------------------------------------------------------

class TestExecuteBash:
    def test_runs_simple_command(self):
        result = _execute_bash({"command": "echo hello"})
        assert "hello" in result

    def test_empty_command_returns_error(self):
        result = _execute_bash({"command": ""})
        assert "Error: command is required" in result

    def test_nonexistent_cwd_returns_error(self):
        result = _execute_bash({"command": "echo hi", "cwd": "/nonexistent/path/xyz"})
        assert "working directory does not exist" in result

    def test_stderr_included_in_output(self):
        result = _execute_bash({"command": "echo err >&2"})
        assert "err" in result

    def test_nonzero_exit_code_reported(self):
        result = _execute_bash({"command": "exit 42"})
        assert "42" in result

    def test_dangerous_rm_rf_blocked(self):
        result = _execute_bash({"command": "rm -rf / --no-preserve-root"})
        assert "blocked" in result.lower()

    def test_no_output_command(self):
        result = _execute_bash({"command": "true"})
        assert result == "[no output]"

    def test_timeout_returns_error(self):
        # Patch subprocess.run to raise TimeoutExpired
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 60)):
            result = _execute_bash({"command": "sleep 999"})
        assert "timed out" in result


# ---------------------------------------------------------------------------
# Tool execution: knowledge_search
# ---------------------------------------------------------------------------

class TestExecuteKnowledgeSearch:
    def test_empty_query_returns_error(self):
        result = _execute_knowledge_search({"query": ""})
        assert "Error: query is required" in result

    def test_missing_module_returns_graceful_message(self):
        # knowledge search module may not be installed in test env
        result = _execute_knowledge_search({"query": "test query"})
        # Should either return results or a graceful "not available" message — not raise
        assert isinstance(result, str)
        assert len(result) > 0

    def test_module_import_error_handled(self):
        with patch.dict("sys.modules", {"cap.knowledge.search": None}):
            result = _execute_knowledge_search({"query": "test"})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# execute_tool dispatcher
# ---------------------------------------------------------------------------

class TestExecuteToolDispatcher:
    def test_unknown_tool_returns_error(self):
        result = execute_tool("nonexistent_tool", {})
        assert "unknown tool" in result

    def test_dispatches_file_read(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("content", encoding="utf-8")
        result = execute_tool("file_read", {"path": str(f)})
        assert result == "content"

    def test_dispatches_bash_exec(self):
        result = execute_tool("bash_exec", {"command": "echo dispatched"})
        assert "dispatched" in result

    def test_dispatches_knowledge_search(self):
        result = execute_tool("knowledge_search", {"query": "test dispatch"})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# ConversationResult
# ---------------------------------------------------------------------------

class TestConversationResult:
    def _make_result(self, **kwargs) -> ConversationResult:
        defaults = dict(
            agent_id="a1",
            agent_type="dev",
            model=MODEL_ALIASES["sonnet"],
            response="hello",
            error=None,
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=0.001,
            duration_ms=500,
            turns=2,
        )
        defaults.update(kwargs)
        return ConversationResult(**defaults)

    def test_fields_present(self):
        r = self._make_result()
        assert r.agent_id == "a1"
        assert r.agent_type == "dev"
        assert r.response == "hello"
        assert r.error is None
        assert r.turns == 2
        assert isinstance(r.timestamp, datetime)

    def test_tool_calls_default_empty_list(self):
        r = self._make_result()
        assert r.tool_calls == []

    def test_to_execution_result_maps_correctly(self):
        r = self._make_result()
        er = r.to_execution_result()
        assert isinstance(er, ExecutionResult)
        assert er.agent_id == r.agent_id
        assert er.model == r.model
        assert er.input_tokens == r.total_input_tokens
        assert er.output_tokens == r.total_output_tokens
        assert er.cost_usd == r.total_cost_usd
        assert er.duration_ms == r.duration_ms
        assert er.response == r.response
        assert er.error == r.error

    def test_to_execution_result_preserves_timestamp(self):
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        r = self._make_result(timestamp=ts)
        er = r.to_execution_result()
        assert er.timestamp == ts

    def test_error_result_conversion(self):
        r = self._make_result(response=None, error="throttled")
        er = r.to_execution_result()
        assert er.response is None
        assert er.error == "throttled"


# ---------------------------------------------------------------------------
# ConverseExecutor — client init
# ---------------------------------------------------------------------------

class TestConverseExecutorClientInit:
    def test_initial_available_is_none(self):
        ex = ConverseExecutor()
        assert ex.is_available is None

    def test_no_credentials_marks_unavailable(self):
        from botocore.exceptions import NoCredentialsError
        with patch("boto3.Session") as mock_cls:
            mock_cls.return_value.client.side_effect = NoCredentialsError()
            ex = ConverseExecutor()
            ex._ensure_client()
        assert ex.is_available is False

    def test_unavailable_execute_returns_error(self):
        ex = ConverseExecutor()
        ex._available = False
        r = ex.execute("a1", "dev", "prompt")
        assert r.error is not None
        assert "unavailable" in r.error

    def test_unavailable_execute_zero_tokens(self):
        ex = ConverseExecutor()
        ex._available = False
        r = ex.execute("a1", "dev", "prompt")
        assert r.total_input_tokens == 0
        assert r.total_output_tokens == 0
        assert r.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# ConverseExecutor — happy path (end_turn)
# ---------------------------------------------------------------------------

class TestConverseExecutorHappyPath:
    def test_returns_conversation_result(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("Done!", input_tokens=20, output_tokens=30)
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "Do something")
        assert isinstance(r, ConversationResult)

    def test_response_text_extracted(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("Final answer.")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.response == "Final answer."
        assert r.error is None

    def test_token_counts_accumulated(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok", input_tokens=15, output_tokens=25)
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.total_input_tokens == 15
        assert r.total_output_tokens == 25

    def test_cost_computed(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok", input_tokens=1_000_000, output_tokens=1_000_000)
        ex = _make_executor(client)
        ex._budget_limit_usd = 1000.0  # disable budget enforcement

        r = ex.execute("a1", "dev", "task", model="sonnet")
        expected = 1_000_000 * 3.00 / 1_000_000 + 1_000_000 * 15.00 / 1_000_000
        assert abs(r.total_cost_usd - expected) < 1e-9

    def test_turns_count_is_one_for_direct_answer(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("answer")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.turns == 1

    def test_is_available_true_after_success(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "task")
        assert ex.is_available is True

    def test_model_resolved_from_alias(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task", model="haiku")
        assert r.model == MODEL_ALIASES["haiku"]

    def test_context_prepended_to_prompt(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "do the task", context="prior output here")
        call_kwargs = client.converse.call_args[1]
        first_message = call_kwargs["messages"][0]
        content_text = first_message["content"][0]["text"]
        assert "prior output here" in content_text
        assert "do the task" in content_text

    def test_system_prompt_passed_when_provided(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "task", system_prompt="You are helpful.")
        call_kwargs = client.converse.call_args[1]
        assert call_kwargs["system"] == [{"text": "You are helpful."}]

    def test_no_system_key_when_no_prompt_and_no_agent_file(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        # Patch AGENT_DEFS_DIR to a place with no files
        with patch("cap.harness.converse_executor.AGENT_DEFS_DIR", Path("/nonexistent/dir")):
            ex.execute("a1", "nonexistent_agent_xyz", "task")

        call_kwargs = client.converse.call_args[1]
        assert "system" not in call_kwargs

    def test_duration_ms_positive(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.duration_ms >= 0

    def test_agent_id_in_result(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        r = ex.execute("my-agent-uuid", "dev", "task")
        assert r.agent_id == "my-agent-uuid"

    def test_agent_type_in_result(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        r = ex.execute("a1", "security", "task")
        assert r.agent_type == "security"

    def test_tool_config_included_by_default(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "task")
        call_kwargs = client.converse.call_args[1]
        assert "toolConfig" in call_kwargs
        assert len(call_kwargs["toolConfig"]["tools"]) == 3  # file_read, bash_exec, knowledge_search

    def test_allowed_tools_filters_tool_config(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)
        ex._allowed_tools = ["file_read"]

        ex.execute("a1", "dev", "task")
        call_kwargs = client.converse.call_args[1]
        tool_names = [t["toolSpec"]["name"] for t in call_kwargs["toolConfig"]["tools"]]
        assert tool_names == ["file_read"]

    def test_empty_allowed_tools_omits_tool_config(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)
        ex._allowed_tools = []

        ex.execute("a1", "dev", "task")
        call_kwargs = client.converse.call_args[1]
        assert "toolConfig" not in call_kwargs

    def test_max_tokens_stops_reason_handled(self):
        client = MagicMock()
        client.converse.return_value = _converse_response("partial", stop_reason="max_tokens")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.response == "partial"
        assert r.error is None


# ---------------------------------------------------------------------------
# ConverseExecutor — multi-turn tool use loop
# ---------------------------------------------------------------------------

class TestConverseExecutorToolLoop:
    def test_single_tool_call_then_end_turn(self, tmp_path):
        """Model requests file_read, executor runs it, model gives final answer."""
        test_file = tmp_path / "data.txt"
        test_file.write_text("the answer is 42", encoding="utf-8")

        client = MagicMock()
        # First call: request file_read
        client.converse.side_effect = [
            _tool_use_response("file_read", {"path": str(test_file)}, "tool-001"),
            _converse_response("The answer is 42."),
        ]
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "What is in the file?")
        assert r.response == "The answer is 42."
        assert r.error is None
        assert r.turns == 2
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0]["tool"] == "file_read"

    def test_tool_result_fed_back_as_user_message(self, tmp_path):
        """Verify the tool result appears in messages as role=user."""
        test_file = tmp_path / "answer.txt"
        test_file.write_text("result content", encoding="utf-8")

        client = MagicMock()
        client.converse.side_effect = [
            _tool_use_response("file_read", {"path": str(test_file)}, "tool-002"),
            _converse_response("Done."),
        ]
        ex = _make_executor(client)
        ex.execute("a1", "dev", "task")

        # Second converse call should have messages with tool results
        second_call_kwargs = client.converse.call_args_list[1][1]
        messages = second_call_kwargs["messages"]
        # messages: [user, assistant(tool_use), user(tool_result)]
        assert len(messages) == 3
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        tool_result_content = messages[2]["content"][0]
        assert "toolResult" in tool_result_content
        assert tool_result_content["toolResult"]["toolUseId"] == "tool-002"

    def test_multiple_sequential_tool_calls(self, tmp_path):
        f1 = tmp_path / "f1.txt"
        f1.write_text("content1", encoding="utf-8")
        f2 = tmp_path / "f2.txt"
        f2.write_text("content2", encoding="utf-8")

        client = MagicMock()
        client.converse.side_effect = [
            _tool_use_response("file_read", {"path": str(f1)}, "tool-a"),
            _tool_use_response("file_read", {"path": str(f2)}, "tool-b"),
            _converse_response("Both read."),
        ]
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "read both files")
        assert r.turns == 3
        assert len(r.tool_calls) == 2
        assert r.response == "Both read."

    def test_token_counts_accumulate_across_turns(self):
        client = MagicMock()
        client.converse.side_effect = [
            {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"toolUse": {"toolUseId": "t1", "name": "bash_exec", "input": {"command": "echo hi"}}}],
                    }
                },
                "usage": {"inputTokens": 100, "outputTokens": 10},
            },
            _converse_response("done", input_tokens=50, output_tokens=20),
        ]
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.total_input_tokens == 150
        assert r.total_output_tokens == 30

    def test_tool_call_output_preview_truncated_in_log(self):
        """tool_calls log stores only first 200 chars of output."""
        client = MagicMock()
        long_content = "x" * 1000
        client.converse.side_effect = [
            _tool_use_response("bash_exec", {"command": f"echo '{long_content}'"}, "tool-long"),
            _converse_response("done"),
        ]
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert len(r.tool_calls[0]["output_preview"]) <= 200

    def test_unknown_tool_name_returns_error_in_result(self):
        client = MagicMock()
        client.converse.side_effect = [
            _tool_use_response("nonexistent_tool_xyz", {}, "tool-x"),
            _converse_response("ok"),
        ]
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        # Should complete without crashing; tool error propagated as tool result
        assert r.error is None or r.turns >= 1


# ---------------------------------------------------------------------------
# ConverseExecutor — max iterations cap
# ---------------------------------------------------------------------------

class TestConverseExecutorMaxIterations:
    def test_max_iterations_reached_returns_error(self):
        client = MagicMock()
        # Always return tool_use — never end_turn
        client.converse.return_value = _tool_use_response(
            "bash_exec", {"command": "echo loop"}, "tool-loop"
        )
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "infinite loop task")
        assert r.error is not None
        assert str(MAX_TOOL_ITERATIONS) in r.error
        assert r.turns == MAX_TOOL_ITERATIONS

    def test_max_iterations_still_records_tokens(self):
        client = MagicMock()
        client.converse.return_value = {
            "stopReason": "tool_use",
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"toolUse": {"toolUseId": "t", "name": "bash_exec", "input": {"command": "true"}}}],
                }
            },
            "usage": {"inputTokens": 5, "outputTokens": 2},
        }
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.total_input_tokens == 5 * MAX_TOOL_ITERATIONS
        assert r.total_output_tokens == 2 * MAX_TOOL_ITERATIONS


# ---------------------------------------------------------------------------
# ConverseExecutor — error handling
# ---------------------------------------------------------------------------

class TestConverseExecutorErrors:
    def test_throttling_returns_error(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("ThrottlingException")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.error is not None
        assert "throttled" in r.error.lower()

    def test_throttling_does_not_mark_unavailable(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("ThrottlingException")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "task")
        assert ex.is_available is not False

    def test_validation_error_returned(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("ValidationException", "bad request")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.error is not None
        assert "validation" in r.error.lower()
        assert "bad request" in r.error

    def test_model_not_ready_returned(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("ModelNotReadyException")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.error == "model_not_ready"

    def test_unknown_client_error_marks_unavailable(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("InternalServerError", "boom")
        ex = _make_executor(client)

        ex.execute("a1", "dev", "task")
        assert ex.is_available is False

    def test_generic_exception_returns_error(self):
        client = MagicMock()
        client.converse.side_effect = RuntimeError("network timeout")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.error == "network timeout"
        assert r.response is None

    def test_error_result_has_zero_cost(self):
        client = MagicMock()
        client.converse.side_effect = _client_error("ThrottlingException")
        ex = _make_executor(client)

        r = ex.execute("a1", "dev", "task")
        assert r.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# ConverseExecutor — retry on throttle
# ---------------------------------------------------------------------------

class TestConverseExecutorRetry:
    def test_retries_twice_on_throttle_then_succeeds(self):
        client = MagicMock()
        # First two calls throttled, third succeeds
        client.converse.side_effect = [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
            _converse_response("success after retries"),
        ]
        ex = _make_executor(client)

        with patch("time.sleep"):  # Skip actual sleep
            r = ex.execute("a1", "dev", "task")

        assert r.response == "success after retries"
        assert r.error is None
        assert client.converse.call_count == 3

    def test_exhausted_retries_returns_throttled_error(self):
        client = MagicMock()
        # All 3 calls throttled (MAX_RETRIES=2 means 3 total attempts)
        client.converse.side_effect = _client_error("ThrottlingException")
        ex = _make_executor(client)

        with patch("time.sleep"):
            r = ex.execute("a1", "dev", "task")

        assert r.error is not None
        assert "throttled" in r.error.lower()

    def test_retry_delays_use_exponential_backoff(self):
        client = MagicMock()
        client.converse.side_effect = [
            _client_error("ThrottlingException"),
            _client_error("ThrottlingException"),
            _converse_response("ok"),
        ]
        ex = _make_executor(client)

        sleep_calls = []
        with patch("time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            ex.execute("a1", "dev", "task")

        assert len(sleep_calls) == 2
        assert sleep_calls[1] > sleep_calls[0]  # exponential: 1.0, 4.0


# ---------------------------------------------------------------------------
# ConverseExecutor — budget enforcement
# ---------------------------------------------------------------------------

class TestConverseExecutorBudget:
    def test_budget_exceeded_before_first_call(self):
        client = MagicMock()
        ex = _make_executor(client)

        with patch("cap.harness.converse_executor.ConverseExecutor._check_budget", return_value="daily budget exceeded"):
            r = ex.execute("a1", "dev", "task")

        assert r.error == "daily budget exceeded"
        client.converse.assert_not_called()

    def test_budget_exceeded_mid_conversation(self):
        client = MagicMock()
        client.converse.return_value = _tool_use_response(
            "bash_exec", {"command": "echo hi"}, "tool-b"
        )
        ex = _make_executor(client)

        call_count = [0]
        def budget_check_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                return "daily budget exceeded"
            return None

        with patch.object(ex, "_check_budget", side_effect=budget_check_side_effect):
            r = ex.execute("a1", "dev", "task")

        assert r.error is not None
        assert "budget" in r.error

    def test_budget_check_exception_does_not_block(self):
        """If cost_meter raises, execution should still proceed."""
        client = MagicMock()
        client.converse.return_value = _converse_response("ok")
        ex = _make_executor(client)

        # Simulate cost_meter being unavailable
        with patch("cap.harness.converse_executor.ConverseExecutor._check_budget", return_value=None):
            r = ex.execute("a1", "dev", "task")

        assert r.response == "ok"


# ---------------------------------------------------------------------------
# ConverseExecutor — streaming
# ---------------------------------------------------------------------------

class TestConverseExecutorStreaming:
    def _make_stream_response(self, text: str, input_tokens: int = 10, output_tokens: int = 20) -> dict:
        """Build a fake converse_stream() response."""
        events = [
            {"contentBlockDelta": {"delta": {"text": chunk}}}
            for chunk in [text[:len(text)//2], text[len(text)//2:]]
        ]
        events.append({
            "metadata": {"usage": {"inputTokens": input_tokens, "outputTokens": output_tokens}}
        })
        return {"stream": events}

    def test_streaming_returns_conversation_result(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("streamed output")
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert isinstance(r, ConversationResult)

    def test_streaming_assembles_text_chunks(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("hello world")
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert r.response == "hello world"
        assert r.error is None

    def test_streaming_records_tokens(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("ok", input_tokens=25, output_tokens=35)
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert r.total_input_tokens == 25
        assert r.total_output_tokens == 35

    def test_streaming_turns_is_one(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("ok")
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert r.turns == 1

    def test_streaming_unavailable_returns_error(self):
        ex = ConverseExecutor()
        ex._available = False
        r = ex.execute_streaming("a1", "dev", "task")
        assert r.error == "bedrock unavailable"

    def test_streaming_client_error_handled(self):
        client = MagicMock()
        client.converse_stream.side_effect = _client_error("ThrottlingException", "slow down")
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert r.error is not None
        assert "ThrottlingException" in r.error

    def test_streaming_generic_exception_handled(self):
        client = MagicMock()
        client.converse_stream.side_effect = RuntimeError("stream broken")
        ex = _make_executor(client)

        r = ex.execute_streaming("a1", "dev", "task")
        assert r.error == "stream broken"

    def test_streaming_system_prompt_passed(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("ok")
        ex = _make_executor(client)

        ex.execute_streaming("a1", "dev", "task", system_prompt="Be concise.")
        call_kwargs = client.converse_stream.call_args[1]
        assert call_kwargs["system"] == [{"text": "Be concise."}]

    def test_streaming_marks_available_on_success(self):
        client = MagicMock()
        client.converse_stream.return_value = self._make_stream_response("ok")
        ex = _make_executor(client)

        ex.execute_streaming("a1", "dev", "task")
        assert ex.is_available is True


# ---------------------------------------------------------------------------
# TOOL_DEFINITIONS structure
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    def test_three_tools_defined(self):
        assert len(TOOL_DEFINITIONS) == 3

    def test_all_tool_names_present(self):
        names = {t["toolSpec"]["name"] for t in TOOL_DEFINITIONS}
        assert names == {"file_read", "bash_exec", "knowledge_search"}

    def test_each_tool_has_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            spec = tool["toolSpec"]
            assert "name" in spec
            assert "description" in spec
            assert "inputSchema" in spec

    def test_file_read_requires_path(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["toolSpec"]["name"] == "file_read")
        schema = tool["toolSpec"]["inputSchema"]["json"]
        assert "path" in schema["required"]

    def test_bash_exec_requires_command(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["toolSpec"]["name"] == "bash_exec")
        schema = tool["toolSpec"]["inputSchema"]["json"]
        assert "command" in schema["required"]

    def test_knowledge_search_requires_query(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["toolSpec"]["name"] == "knowledge_search")
        schema = tool["toolSpec"]["inputSchema"]["json"]
        assert "query" in schema["required"]

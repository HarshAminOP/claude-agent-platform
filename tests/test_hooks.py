"""Tests for lifecycle hooks module."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.hooks import (
    HookType, HookContext, register_hook, unregister_hook,
    emit_hook, clear_hooks, register_builtin_hooks,
    correction_injection_hook, tool_restriction_hook, budget_check_hook,
)


@pytest.fixture(autouse=True)
def clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


def test_register_and_emit():
    results = []
    def handler(ctx):
        results.append(ctx.agent_id)
        return "handled"

    register_hook(HookType.before_agent_spawn, handler)
    ctx = HookContext(hook_type=HookType.before_agent_spawn, agent_id="test-agent")
    out = emit_hook(HookType.before_agent_spawn, ctx)
    assert results == ["test-agent"]
    assert out == ["handled"]


def test_unregister():
    def handler(ctx):
        return "x"

    register_hook(HookType.before_agent_spawn, handler)
    unregister_hook(HookType.before_agent_spawn, handler)
    ctx = HookContext(hook_type=HookType.before_agent_spawn)
    out = emit_hook(HookType.before_agent_spawn, ctx)
    assert out == []


def test_multiple_handlers_run_in_order():
    order = []
    def h1(ctx): order.append(1)
    def h2(ctx): order.append(2)

    register_hook(HookType.after_agent_complete, h1)
    register_hook(HookType.after_agent_complete, h2)
    emit_hook(HookType.after_agent_complete, HookContext(hook_type=HookType.after_agent_complete))
    assert order == [1, 2]


def test_budget_check_hook_warns_at_80():
    ctx = HookContext(hook_type=HookType.before_agent_spawn, budget_pct=85.0)
    result = budget_check_hook(ctx)
    assert result is not None
    assert ctx.metadata.get("budget_warning") is True


def test_budget_check_hook_blocks_at_100():
    ctx = HookContext(hook_type=HookType.before_agent_spawn, budget_pct=100.0)
    with pytest.raises(RuntimeError, match="Budget exceeded"):
        budget_check_hook(ctx)


def test_budget_check_hook_passes_below_80():
    ctx = HookContext(hook_type=HookType.before_agent_spawn, budget_pct=50.0)
    result = budget_check_hook(ctx)
    assert result is None
    assert "budget_warning" not in ctx.metadata


def test_tool_restriction_hook_blocks():
    ctx = HookContext(
        hook_type=HookType.before_agent_spawn,
        metadata={"requested_tools": ["rm", "kubectl"], "denied_tools": ["rm", "terraform"]},
    )
    with pytest.raises(PermissionError, match="Denied tools"):
        tool_restriction_hook(ctx)
    assert "rm" in ctx.metadata["tool_violations"]


def test_tool_restriction_hook_passes():
    ctx = HookContext(
        hook_type=HookType.before_agent_spawn,
        metadata={"requested_tools": ["read", "write"], "denied_tools": ["rm"]},
    )
    result = tool_restriction_hook(ctx)
    assert result is None


def test_correction_injection_hook():
    def mock_recall(query, agent_id):
        return "Don't use shell=True"

    ctx = HookContext(
        hook_type=HookType.before_agent_spawn,
        agent_id="dev",
        prompt="Implement feature X",
        metadata={"session_recall_fn": mock_recall},
    )
    result = correction_injection_hook(ctx)
    assert result == "Don't use shell=True"
    assert "[SYSTEM] Prior corrections:" in ctx.prompt


def test_register_builtin_hooks_idempotent():
    register_builtin_hooks()
    register_builtin_hooks()  # Should not double-register
    ctx = HookContext(hook_type=HookType.before_agent_spawn, budget_pct=50.0)
    results = emit_hook(HookType.before_agent_spawn, ctx)
    # 3 built-in hooks, all should return None at 50% budget
    assert len(results) == 3


def test_emit_hook_captures_errors_but_reraises_blocking():
    def failing_handler(ctx):
        raise ValueError("oops")

    def blocking_handler(ctx):
        raise PermissionError("denied")

    register_hook(HookType.before_push, failing_handler)
    register_hook(HookType.before_push, blocking_handler)
    ctx = HookContext(hook_type=HookType.before_push)

    with pytest.raises(PermissionError):
        emit_hook(HookType.before_push, ctx)

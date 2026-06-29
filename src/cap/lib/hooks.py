"""Lifecycle hooks for the agent orchestration system.

Registry pattern allowing the orchestrator to invoke handlers at key points.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List


class HookType(str, Enum):
    before_agent_spawn = "before_agent_spawn"
    after_agent_complete = "after_agent_complete"
    before_push = "before_push"
    before_apply = "before_apply"
    on_budget_warning = "on_budget_warning"
    on_budget_exceeded = "on_budget_exceeded"
    on_correction = "on_correction"
    on_review_reject = "on_review_reject"


@dataclass
class HookContext:
    hook_type: HookType
    agent_id: str = ""
    prompt: str = ""
    output: Any = None
    budget_pct: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# Registry: hook_type -> ordered list of handlers
_registry: Dict[HookType, List[Callable[[HookContext], Any]]] = {
    ht: [] for ht in HookType
}


def register_hook(hook_type: HookType, handler: Callable[[HookContext], Any]) -> None:
    """Register a handler for a lifecycle hook type."""
    _registry[hook_type].append(handler)


def unregister_hook(hook_type: HookType, handler: Callable[[HookContext], Any]) -> None:
    """Remove a previously registered handler."""
    _registry[hook_type] = [h for h in _registry[hook_type] if h is not handler]


def emit_hook(hook_type: HookType, context: HookContext) -> List[Any]:
    """Emit a hook, invoking all handlers in order. Returns list of results."""
    results: List[Any] = []
    for handler in _registry[hook_type]:
        results.append(handler(context))
    return results


def clear_hooks(hook_type: HookType | None = None) -> None:
    """Clear handlers for a hook type, or all if None."""
    if hook_type:
        _registry[hook_type] = []
    else:
        for ht in HookType:
            _registry[ht] = []


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

def correction_injection_hook(ctx: HookContext) -> Any:
    """Query session memory for corrections and inject into agent prompt."""
    recall_fn = ctx.metadata.get("session_recall_fn")
    if not recall_fn:
        return None
    corrections = recall_fn(query="corrections", agent_id=ctx.agent_id)
    if corrections:
        ctx.metadata["injected_corrections"] = corrections
        ctx.prompt += f"\n\n[SYSTEM] Prior corrections:\n{corrections}"
    return corrections


def tool_restriction_hook(ctx: HookContext) -> Any:
    """Block agent if it requests denied tools."""
    requested = set(ctx.metadata.get("requested_tools", []))
    denied = set(ctx.metadata.get("denied_tools", []))
    violations = requested & denied
    if violations:
        ctx.metadata["tool_violations"] = list(violations)
        raise PermissionError(f"Denied tools requested: {violations}")
    return None


def budget_check_hook(ctx: HookContext) -> Any:
    """Block spawn if budget >= 100%, warn if >= 80%."""
    if ctx.budget_pct >= 100.0:
        raise RuntimeError(
            f"Budget exceeded ({ctx.budget_pct:.0f}%) — agent spawn blocked."
        )
    if ctx.budget_pct >= 80.0:
        ctx.metadata["budget_warning"] = True
        return {"warning": f"Budget at {ctx.budget_pct:.0f}%"}
    return None


# Auto-register built-in hooks
register_hook(HookType.before_agent_spawn, correction_injection_hook)
register_hook(HookType.before_agent_spawn, tool_restriction_hook)
register_hook(HookType.before_agent_spawn, budget_check_hook)

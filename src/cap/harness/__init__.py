"""Agent harness — lifecycle management for CAP specialist agents."""

from cap.harness.executor import AgentExecutor, ExecutionResult
from cap.harness.agent_store import (
    AgentRecord,
    spawn_agent,
    get_agent,
    list_agents,
    update_agent,
    terminate_agent,
    record_execution,
    cleanup_stale,
)
from cap.harness.cost_meter import record_execution as record_cost
from cap.harness.hooks import (
    hooks_route,
    hooks_pre_task,
    hooks_post_task,
)
from cap.harness.swarm import (
    swarm_init,
    swarm_status,
    swarm_shutdown,
)
from cap.harness.coordination import (
    coordination_assign,
    coordination_release,
)
from cap.harness.governance import (
    HarnessPolicy,
    load_policy,
    check_dangerous,
    enforce_budget,
    generate_manifest,
    write_manifest,
    verify_manifest,
    record_audit,
)
from cap.harness.validation import (
    validate_identifier,
    validate_text,
    validate_path,
    sanitize_for_storage,
)

__all__ = [
    # Executor
    "AgentExecutor",
    "ExecutionResult",
    # Agent store
    "AgentRecord",
    "spawn_agent",
    "get_agent",
    "list_agents",
    "update_agent",
    "terminate_agent",
    "record_execution",
    "cleanup_stale",
    # Cost meter alias
    "record_cost",
    # Hooks
    "hooks_route",
    "hooks_pre_task",
    "hooks_post_task",
    # Swarm
    "swarm_init",
    "swarm_status",
    "swarm_shutdown",
    # Coordination
    "coordination_assign",
    "coordination_release",
    # Governance
    "HarnessPolicy",
    "load_policy",
    "check_dangerous",
    "enforce_budget",
    "generate_manifest",
    "write_manifest",
    "verify_manifest",
    "record_audit",
    # Validation
    "validate_identifier",
    "validate_text",
    "validate_path",
    "sanitize_for_storage",
]

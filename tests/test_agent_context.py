"""Tests for cap.lib.agent_context — SharedState and AgentContext."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.agent_context import (
    AgentContext,
    SharedState,
    create_agent_context,
    _ensure_table,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return a fresh per-test SQLite path under a temp directory."""
    return str(tmp_path / "test_cap.db")


@pytest.fixture
def session_id() -> str:
    return "session-test-001"


@pytest.fixture
def shared_state(db_path: str, session_id: str) -> SharedState:
    return SharedState(session_id=session_id, db_path=db_path)


# ---------------------------------------------------------------------------
# SharedState — basic CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get(shared_state: SharedState) -> None:
    await shared_state.set("foo", "bar")
    value = await shared_state.get("foo")
    assert value == "bar"


@pytest.mark.asyncio
async def test_get_missing_key_returns_none(shared_state: SharedState) -> None:
    result = await shared_state.get("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_set_overwrites_existing(shared_state: SharedState) -> None:
    await shared_state.set("counter", 1)
    await shared_state.set("counter", 2)
    assert await shared_state.get("counter") == 2


@pytest.mark.asyncio
async def test_get_all_returns_all_keys(shared_state: SharedState) -> None:
    await shared_state.set("a", 1)
    await shared_state.set("b", 2)
    all_state = await shared_state.get_all()
    assert all_state == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_get_all_is_a_copy(shared_state: SharedState) -> None:
    await shared_state.set("x", 10)
    snapshot = await shared_state.get_all()
    snapshot["x"] = 99
    assert await shared_state.get("x") == 10


@pytest.mark.asyncio
async def test_delete_removes_key(shared_state: SharedState) -> None:
    await shared_state.set("to_del", "value")
    await shared_state.delete("to_del")
    assert await shared_state.get("to_del") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(shared_state: SharedState) -> None:
    # Should not raise
    await shared_state.delete("ghost_key")


@pytest.mark.asyncio
async def test_keys_returns_sorted_list(shared_state: SharedState) -> None:
    await shared_state.set("zebra", 1)
    await shared_state.set("apple", 2)
    await shared_state.set("mango", 3)
    result = await shared_state.keys()
    assert result == ["apple", "mango", "zebra"]


@pytest.mark.asyncio
async def test_keys_with_prefix(shared_state: SharedState) -> None:
    await shared_state.set("findings.security.a1", {})
    await shared_state.set("findings.cost.a2", {})
    await shared_state.set("other.key", "x")
    result = await shared_state.keys(prefix="findings.")
    assert result == ["findings.cost.a2", "findings.security.a1"]


@pytest.mark.asyncio
async def test_clear_removes_all_keys(shared_state: SharedState) -> None:
    await shared_state.set("k1", 1)
    await shared_state.set("k2", 2)
    await shared_state.clear()
    assert await shared_state.get_all() == {}


# ---------------------------------------------------------------------------
# SharedState — persistence (write-through to SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_across_instances(
    db_path: str, session_id: str
) -> None:
    """Values written by one SharedState instance must survive to a new one."""
    s1 = SharedState(session_id=session_id, db_path=db_path)
    await s1.set("persistent_key", {"nested": True})

    s2 = SharedState(session_id=session_id, db_path=db_path)
    value = await s2.get("persistent_key")
    assert value == {"nested": True}


@pytest.mark.asyncio
async def test_session_isolation(db_path: str) -> None:
    """Two different sessions must not see each other's keys."""
    s1 = SharedState(session_id="sess-A", db_path=db_path)
    s2 = SharedState(session_id="sess-B", db_path=db_path)

    await s1.set("shared_key", "session_a_value")
    result = await s2.get("shared_key")
    assert result is None


@pytest.mark.asyncio
async def test_delete_is_persisted(db_path: str, session_id: str) -> None:
    s1 = SharedState(session_id=session_id, db_path=db_path)
    await s1.set("del_me", "yes")
    await s1.delete("del_me")

    # A fresh instance should not load the deleted key.
    s2 = SharedState(session_id=session_id, db_path=db_path)
    assert await s2.get("del_me") is None


@pytest.mark.asyncio
async def test_clear_is_persisted(db_path: str, session_id: str) -> None:
    s1 = SharedState(session_id=session_id, db_path=db_path)
    await s1.set("k", "v")
    await s1.clear()

    s2 = SharedState(session_id=session_id, db_path=db_path)
    assert await s2.get_all() == {}


# ---------------------------------------------------------------------------
# SharedState — subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_exact_key(shared_state: SharedState) -> None:
    received: list[tuple] = []

    async def cb(key: str, value) -> None:
        received.append((key, value))

    await shared_state.subscribe("my.key", cb)
    await shared_state.set("my.key", 42)
    assert received == [("my.key", 42)]


@pytest.mark.asyncio
async def test_subscribe_wildcard_pattern(shared_state: SharedState) -> None:
    received: list[str] = []

    async def cb(key: str, value) -> None:
        received.append(key)

    await shared_state.subscribe("findings.*", cb)
    await shared_state.set("findings.security", {"sev": "high"})
    await shared_state.set("findings.cost", {"usd": 10})
    await shared_state.set("other.key", "ignored")

    assert "findings.security" in received
    assert "findings.cost" in received
    assert "other.key" not in received


@pytest.mark.asyncio
async def test_subscribe_non_matching_key_not_called(
    shared_state: SharedState,
) -> None:
    called = []

    async def cb(key: str, value) -> None:
        called.append(key)

    await shared_state.subscribe("specific.key", cb)
    await shared_state.set("other.key", "value")
    assert called == []


@pytest.mark.asyncio
async def test_subscribe_sync_callback(shared_state: SharedState) -> None:
    collected = []

    def sync_cb(key: str, value) -> None:
        collected.append(value)

    await shared_state.subscribe("sync.*", sync_cb)
    await shared_state.set("sync.test", 99)
    assert collected == [99]


@pytest.mark.asyncio
async def test_subscribe_callback_exception_does_not_break_set(
    shared_state: SharedState,
) -> None:
    """A crashing subscriber must not prevent the value from being stored."""

    async def bad_cb(key: str, value) -> None:
        raise RuntimeError("subscriber error")

    await shared_state.subscribe("*", bad_cb)
    # Should not raise
    await shared_state.set("resilient", "value")
    assert await shared_state.get("resilient") == "value"


# ---------------------------------------------------------------------------
# SharedState — validation
# ---------------------------------------------------------------------------


def test_empty_session_id_raises(db_path: str) -> None:
    with pytest.raises(ValueError, match="session_id"):
        SharedState(session_id="", db_path=db_path)


@pytest.mark.asyncio
async def test_set_empty_key_raises(shared_state: SharedState) -> None:
    with pytest.raises(ValueError, match="key"):
        await shared_state.set("", "value")


@pytest.mark.asyncio
async def test_set_non_json_serialisable_raises(shared_state: SharedState) -> None:
    with pytest.raises(TypeError, match="JSON-serialisable"):
        await shared_state.set("bad", object())


@pytest.mark.asyncio
async def test_subscribe_empty_pattern_raises(shared_state: SharedState) -> None:
    with pytest.raises(ValueError, match="pattern"):
        await shared_state.subscribe("", lambda k, v: None)


@pytest.mark.asyncio
async def test_subscribe_non_callable_raises(shared_state: SharedState) -> None:
    with pytest.raises(TypeError, match="callable"):
        await shared_state.subscribe("*", "not_a_callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SharedState — concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sets_are_safe(shared_state: SharedState) -> None:
    """Multiple concurrent writers must not corrupt state."""

    async def write_many(prefix: str) -> None:
        for i in range(20):
            await shared_state.set(f"{prefix}.{i}", i)

    await asyncio.gather(write_many("a"), write_many("b"), write_many("c"))

    all_keys = await shared_state.keys()
    # 3 prefixes × 20 keys each
    assert len(all_keys) == 60


# ---------------------------------------------------------------------------
# AgentContext — basic usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_context_sets_fields(
    shared_state: SharedState,
) -> None:
    ctx = create_agent_context(
        agent_id="agent-1",
        agent_type="dev",
        task="Implement feature X",
        workspace="/repo/myservice",
        session_id="session-test-001",
        shared_state=shared_state,
    )
    assert ctx.agent_id == "agent-1"
    assert ctx.agent_type == "dev"
    assert ctx.task == "Implement feature X"
    assert ctx.workspace == "/repo/myservice"
    assert ctx.session_id == "session-test-001"
    assert ctx.local_state == {}
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_get_shared_delegates_to_shared_state(
    shared_state: SharedState,
) -> None:
    ctx = create_agent_context(
        agent_id="a1", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    await shared_state.set("some.key", "hello")
    assert await ctx.get_shared("some.key") == "hello"


@pytest.mark.asyncio
async def test_set_shared_writes_with_agent_publisher(
    db_path: str,
) -> None:
    ss = SharedState(session_id="s1", db_path=db_path)
    ctx = create_agent_context(
        agent_id="writer-agent", agent_type="dev", task="t", workspace="/w",
        session_id="s1", shared_state=ss,
    )
    await ctx.set_shared("metric", 42)
    assert await ss.get("metric") == 42


@pytest.mark.asyncio
async def test_publish_stores_finding_in_shared_state(
    shared_state: SharedState,
) -> None:
    ctx = create_agent_context(
        agent_id="sec-agent", agent_type="security", task="audit", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    await ctx.publish("vuln", {"severity": "high", "cve": "CVE-2024-0001"})

    key = "findings.vuln.sec-agent"
    stored = await shared_state.get(key)
    assert stored is not None
    assert stored["agent_id"] == "sec-agent"
    assert stored["topic"] == "vuln"
    assert stored["payload"]["severity"] == "high"
    assert "published_at" in stored


@pytest.mark.asyncio
async def test_publish_empty_topic_raises(shared_state: SharedState) -> None:
    ctx = create_agent_context(
        agent_id="a1", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    with pytest.raises(ValueError, match="topic"):
        await ctx.publish("", {"data": 1})


@pytest.mark.asyncio
async def test_publish_non_dict_payload_raises(shared_state: SharedState) -> None:
    ctx = create_agent_context(
        agent_id="a1", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    with pytest.raises(TypeError, match="payload"):
        await ctx.publish("topic", "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AgentContext — inbox / messaging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_returns_message(shared_state: SharedState) -> None:
    ctx = create_agent_context(
        agent_id="receiver", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    await ctx.inbox.put({"type": "ping", "data": 1})
    msg = await ctx.receive(timeout=1.0)
    assert msg == {"type": "ping", "data": 1}


@pytest.mark.asyncio
async def test_receive_timeout_returns_none(shared_state: SharedState) -> None:
    ctx = create_agent_context(
        agent_id="slow-receiver", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    msg = await ctx.receive(timeout=0.05)
    assert msg is None


@pytest.mark.asyncio
async def test_send_to_delivers_message(shared_state: SharedState) -> None:
    sender = create_agent_context(
        agent_id="sender", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    receiver = create_agent_context(
        agent_id="receiver", agent_type="sre", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    await sender.send_to(receiver.inbox, {"type": "request_info", "key": "db_url"})

    msg = await receiver.receive(timeout=1.0)
    assert msg is not None
    assert msg["type"] == "request_info"
    assert msg["sender_id"] == "sender"
    assert "sent_at" in msg


@pytest.mark.asyncio
async def test_send_to_non_dict_raises(shared_state: SharedState) -> None:
    sender = create_agent_context(
        agent_id="s", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
    )
    with pytest.raises(TypeError, match="dict"):
        await sender.send_to(asyncio.Queue(), "not a dict")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_bus_is_used(shared_state: SharedState) -> None:
    custom_q: asyncio.Queue = asyncio.Queue()
    ctx = create_agent_context(
        agent_id="a1", agent_type="dev", task="t", workspace="/w",
        session_id="session-test-001", shared_state=shared_state,
        bus=custom_q,
    )
    assert ctx.inbox is custom_q


# ---------------------------------------------------------------------------
# create_agent_context — validation
# ---------------------------------------------------------------------------


def test_factory_rejects_empty_agent_id(shared_state: SharedState) -> None:
    with pytest.raises(ValueError, match="agent_id"):
        create_agent_context(
            agent_id="", agent_type="dev", task="t", workspace="/w",
            session_id="s", shared_state=shared_state,
        )


def test_factory_rejects_wrong_shared_state_type() -> None:
    with pytest.raises(TypeError, match="SharedState"):
        create_agent_context(
            agent_id="a", agent_type="dev", task="t", workspace="/w",
            session_id="s", shared_state="not_a_shared_state",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Multi-agent coordination scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_agents_share_state(db_path: str) -> None:
    """Two agents reading/writing shared state see each other's values."""
    ss = SharedState(session_id="multi-agent-session", db_path=db_path)

    dev = create_agent_context(
        agent_id="dev-1", agent_type="dev", task="build", workspace="/repo",
        session_id="multi-agent-session", shared_state=ss,
    )
    sec = create_agent_context(
        agent_id="sec-1", agent_type="security", task="audit", workspace="/repo",
        session_id="multi-agent-session", shared_state=ss,
    )

    await dev.set_shared("build_artifact", "app:v1.2.3")
    await sec.publish("scan_result", {"status": "clean", "artifact": "app:v1.2.3"})

    artifact = await sec.get_shared("build_artifact")
    assert artifact == "app:v1.2.3"

    scan = await dev.get_shared("findings.scan_result.sec-1")
    assert scan["payload"]["status"] == "clean"


@pytest.mark.asyncio
async def test_local_state_is_isolated(db_path: str) -> None:
    """local_state of one agent must not be visible to another."""
    ss = SharedState(session_id="isolation-session", db_path=db_path)

    a1 = create_agent_context(
        agent_id="a1", agent_type="dev", task="t", workspace="/w",
        session_id="isolation-session", shared_state=ss,
    )
    a2 = create_agent_context(
        agent_id="a2", agent_type="dev", task="t", workspace="/w",
        session_id="isolation-session", shared_state=ss,
    )

    a1.local_state["secret"] = "agent1_private"

    # a2's local_state is a separate dict
    assert "secret" not in a2.local_state

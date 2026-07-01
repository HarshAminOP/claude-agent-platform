"""Tests for cap.lib.agent_bus — pub/sub message bus."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.agent_bus import AgentBus, BusMessage


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return str(Path(tempfile.mkdtemp()) / "test_bus.db")


def make_bus(session_id: str = "sess-test") -> AgentBus:
    return AgentBus(session_id=session_id, db_path=_db_path())


# ---------------------------------------------------------------------------
# BusMessage unit tests
# ---------------------------------------------------------------------------

class TestBusMessage:
    def test_to_row_and_from_row_roundtrip(self):
        import sqlite3
        msg = BusMessage(
            id="id-1",
            sender="dev-1",
            sender_type="dev",
            topic="findings.dev",
            payload={"key": "value"},
            timestamp=1000.0,
            session_id="s1",
            reply_to=None,
        )
        row_tuple = msg.to_row()
        # Simulate sqlite3.Row via a dict-like object
        cols = ["id", "session_id", "sender", "sender_type", "topic",
                "payload_json", "timestamp", "reply_to"]
        row_dict = dict(zip(cols, row_tuple))

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE t (id, session_id, sender, sender_type, topic, "
            "payload_json, timestamp, reply_to)"
        )
        conn.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)", row_tuple)
        row = conn.execute("SELECT * FROM t").fetchone()

        rebuilt = BusMessage.from_row(row)
        assert rebuilt.id == msg.id
        assert rebuilt.payload == msg.payload
        assert rebuilt.reply_to is None

    def test_reply_to_roundtrip(self):
        import sqlite3, json
        msg = BusMessage(
            id="id-2", sender="s", sender_type="st",
            topic="t", payload={}, timestamp=1.0,
            session_id="s1", reply_to="parent-id",
        )
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE t (id, session_id, sender, sender_type, topic, "
            "payload_json, timestamp, reply_to)"
        )
        conn.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)", msg.to_row())
        row = conn.execute("SELECT * FROM t").fetchone()
        rebuilt = BusMessage.from_row(row)
        assert rebuilt.reply_to == "parent-id"


# ---------------------------------------------------------------------------
# AgentBus — subscribe / publish / get_messages
# ---------------------------------------------------------------------------

class TestSubscribePublish:
    @pytest.mark.asyncio
    async def test_subscribe_and_receive(self):
        bus = make_bus()
        await bus.subscribe("agent-a", "findings.*")
        msg_id = await bus.publish("dev-1", "dev", "findings.dev", {"x": 1})

        msgs = await bus.get_messages("agent-a")
        assert len(msgs) == 1
        assert msgs[0].id == msg_id
        assert msgs[0].topic == "findings.dev"
        assert msgs[0].payload == {"x": 1}

    @pytest.mark.asyncio
    async def test_wildcard_pattern_matches_deep_topics(self):
        bus = make_bus()
        await bus.subscribe("monitor", "status.*")
        await bus.publish("dev-1", "dev", "status.dev.step-1", {"done": True})

        msgs = await bus.get_messages("monitor")
        assert len(msgs) == 1
        assert msgs[0].topic == "status.dev.step-1"

    @pytest.mark.asyncio
    async def test_no_match_delivers_nothing(self):
        bus = make_bus()
        await bus.subscribe("agent-b", "findings.*")
        await bus.publish("dev-1", "dev", "status.dev", {"step": 1})

        msgs = await bus.get_messages("agent-b")
        assert msgs == []

    @pytest.mark.asyncio
    async def test_multiple_subscribers_each_get_copy(self):
        bus = make_bus()
        await bus.subscribe("consumer-1", "findings.*")
        await bus.subscribe("consumer-2", "findings.*")
        await bus.publish("sec-1", "security", "findings.security", {"vuln": "XSS"})

        m1 = await bus.get_messages("consumer-1")
        m2 = await bus.get_messages("consumer-2")
        assert len(m1) == 1
        assert len(m2) == 1
        assert m1[0].id == m2[0].id  # same message, different queue slots

    @pytest.mark.asyncio
    async def test_publish_increments_message_count(self):
        bus = make_bus()
        assert bus.message_count == 0
        await bus.publish("dev-1", "dev", "findings.dev", {})
        await bus.publish("dev-1", "dev", "findings.dev", {})
        assert bus.message_count == 2

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = make_bus()
        await bus.subscribe("watcher", "status.*")
        await bus.unsubscribe("watcher", "status.*")
        await bus.publish("dev-1", "dev", "status.dev", {})

        msgs = await bus.get_messages("watcher")
        assert msgs == []


# ---------------------------------------------------------------------------
# AgentBus — broadcast
# ---------------------------------------------------------------------------

class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_reaches_all_registered_queues(self):
        bus = make_bus()
        # Create queues for three agents
        bus.get_delivery_queue("a1")
        bus.get_delivery_queue("a2")
        bus.get_delivery_queue("a3")

        await bus.broadcast("orchestrator", "orchestrator", {"msg": "shutdown"})

        for agent_id in ("a1", "a2", "a3"):
            msgs = await bus.get_messages(agent_id)
            assert len(msgs) == 1
            assert msgs[0].topic == "broadcast"
            assert msgs[0].payload == {"msg": "shutdown"}

    @pytest.mark.asyncio
    async def test_broadcast_increments_count(self):
        bus = make_bus()
        bus.get_delivery_queue("x")
        await bus.broadcast("orch", "orchestrator", {})
        assert bus.message_count == 1


# ---------------------------------------------------------------------------
# AgentBus — request / respond
# ---------------------------------------------------------------------------

class TestRequestResponse:
    @pytest.mark.asyncio
    async def test_request_response_roundtrip(self):
        bus = make_bus()
        # security agent subscribes to request.security
        await bus.subscribe("sec-1", "request.security")

        async def _responder():
            # Wait for the request to arrive in sec-1's queue
            msgs = await bus.get_messages("sec-1", timeout=2.0)
            assert len(msgs) == 1, "security agent should receive the request"
            req = msgs[0]
            await bus.respond(
                "sec-1", "security", req.id, {"safe": True, "notes": "ok"}
            )

        # Fire responder and requester concurrently
        result, _ = await asyncio.gather(
            bus.request("dev-1", "dev", "security", "is this safe?", timeout=5.0),
            _responder(),
        )
        assert result == {"safe": True, "notes": "ok"}

    @pytest.mark.asyncio
    async def test_request_times_out_when_no_responder(self):
        bus = make_bus()
        result = await bus.request(
            "dev-1", "dev", "security", "any answer?", timeout=0.1
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_respond_requires_nonempty_request_id(self):
        bus = make_bus()
        with pytest.raises(ValueError, match="request_message_id"):
            await bus.respond("sec-1", "security", "", {"ok": True})


# ---------------------------------------------------------------------------
# AgentBus — get_messages with timeout
# ---------------------------------------------------------------------------

class TestGetMessages:
    @pytest.mark.asyncio
    async def test_get_messages_timeout_returns_empty_when_nothing_arrives(self):
        bus = make_bus()
        msgs = await bus.get_messages("lonely-agent", timeout=0.05)
        assert msgs == []

    @pytest.mark.asyncio
    async def test_get_messages_zero_timeout_nonblocking(self):
        bus = make_bus()
        bus.get_delivery_queue("idle-agent")
        msgs = await bus.get_messages("idle-agent", timeout=0.0)
        assert msgs == []

    @pytest.mark.asyncio
    async def test_get_messages_drains_entire_queue(self):
        bus = make_bus()
        await bus.subscribe("batcher", "findings.*")
        for i in range(5):
            await bus.publish("dev-1", "dev", "findings.dev", {"i": i})

        msgs = await bus.get_messages("batcher")
        assert len(msgs) == 5
        # Second call should return nothing
        msgs2 = await bus.get_messages("batcher")
        assert msgs2 == []


# ---------------------------------------------------------------------------
# AgentBus — SQLite persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    @pytest.mark.asyncio
    async def test_schema_created_on_init(self):
        import sqlite3
        db = _db_path()
        _bus = AgentBus(session_id="schema-test", db_path=db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "bus_messages" in tables
        assert "bus_subscriptions" in tables

    @pytest.mark.asyncio
    async def test_subscriptions_persisted_and_reloaded(self):
        db = _db_path()
        bus1 = AgentBus(session_id="persist-sess", db_path=db)
        await bus1.subscribe("agent-z", "handoff.*")

        # Re-open the bus — subscriptions should reload
        bus2 = AgentBus(session_id="persist-sess", db_path=db)
        # The agent should have a queue entry
        assert "agent-z" in bus2._queues

    @pytest.mark.asyncio
    async def test_message_written_to_db(self):
        import sqlite3, asyncio, time
        db = _db_path()
        bus = AgentBus(session_id="db-write-sess", db_path=db)
        await bus.subscribe("reader", "findings.*")
        await bus.publish("dev-1", "dev", "findings.dev", {"key": "val"})

        # Give run_in_executor a tick to complete
        await asyncio.sleep(0.05)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM bus_messages WHERE session_id='db-write-sess'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["topic"] == "findings.dev"


# ---------------------------------------------------------------------------
# AgentBus — topic history
# ---------------------------------------------------------------------------

class TestTopicHistory:
    @pytest.mark.asyncio
    async def test_get_topic_history_filters_by_pattern(self):
        import asyncio
        db = _db_path()
        bus = AgentBus(session_id="hist-sess", db_path=db)
        await bus.subscribe("rx", "findings.*")
        await bus.publish("dev-1", "dev", "findings.dev", {"a": 1})
        await bus.publish("dev-1", "dev", "findings.security", {"b": 2})
        await bus.publish("dev-1", "dev", "status.dev", {"c": 3})

        await asyncio.sleep(0.05)  # let executor writes flush

        history = await bus.get_topic_history("findings.*", limit=50)
        topics = {m.topic for m in history}
        assert "findings.dev" in topics
        assert "findings.security" in topics
        assert "status.dev" not in topics

    @pytest.mark.asyncio
    async def test_get_topic_history_respects_limit(self):
        import asyncio
        db = _db_path()
        bus = AgentBus(session_id="hist-limit", db_path=db)
        await bus.subscribe("rx", "findings.*")
        for i in range(10):
            await bus.publish("dev-1", "dev", "findings.dev", {"i": i})

        await asyncio.sleep(0.05)

        history = await bus.get_topic_history("findings.*", limit=3)
        assert len(history) <= 3

    @pytest.mark.asyncio
    async def test_get_topic_history_invalid_limit(self):
        bus = make_bus()
        with pytest.raises(ValueError, match="limit"):
            await bus.get_topic_history("findings.*", limit=0)


# ---------------------------------------------------------------------------
# AgentBus — drain
# ---------------------------------------------------------------------------

class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_cancels_pending_futures(self):
        bus = make_bus()
        # Start a request but don't respond to it
        task = asyncio.create_task(
            bus.request("dev-1", "dev", "security", "q?", timeout=10.0)
        )
        await asyncio.sleep(0)  # let task reach the await
        await bus.drain()
        # drain() cancels the internal future; request() propagates as
        # CancelledError through asyncio.shield/wait_for — that is the
        # expected behaviour.  The task should finish (cancelled or None).
        try:
            result = await asyncio.wait_for(task, timeout=1.0)
            assert result is None
        except asyncio.CancelledError:
            pass  # drain triggered cancellation — acceptable outcome

    @pytest.mark.asyncio
    async def test_drain_clears_queues(self):
        bus = make_bus()
        await bus.subscribe("cleaner", "findings.*")
        await bus.publish("dev-1", "dev", "findings.dev", {})
        await bus.drain()
        # Queue should be empty after drain
        msgs = await bus.get_messages("cleaner")
        assert msgs == []


# ---------------------------------------------------------------------------
# AgentBus — validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_empty_session_id_raises(self):
        with pytest.raises(ValueError, match="session_id"):
            AgentBus(session_id="", db_path=_db_path())

    @pytest.mark.asyncio
    async def test_publish_empty_topic_raises(self):
        bus = make_bus()
        with pytest.raises(ValueError, match="topic"):
            await bus.publish("dev-1", "dev", "", {})

    @pytest.mark.asyncio
    async def test_subscribe_empty_agent_raises(self):
        bus = make_bus()
        with pytest.raises(ValueError):
            await bus.subscribe("", "findings.*")

    @pytest.mark.asyncio
    async def test_subscribe_empty_pattern_raises(self):
        bus = make_bus()
        with pytest.raises(ValueError):
            await bus.subscribe("dev-1", "")

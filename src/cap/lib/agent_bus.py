"""Pub/sub message bus for inter-agent communication.

Agents publish findings, request info from specialists, and broadcast
status updates without tight coupling. Uses in-process asyncio queues
for delivery and SQLite for persistence / audit trail.

Topics:
    findings.*   — discoveries published by agents
    request.*    — info requests directed at a specialist type
    status.*     — progress updates  (e.g. status.dev.step-1)
    handoff.*    — agent-to-agent hand-off signals

Usage::

    bus = AgentBus(session_id="abc", db_path="/tmp/cap.db")
    await bus.subscribe("security-1", "findings.*")
    msg_id = await bus.publish("dev-1", "dev", "findings.dev", {"vuln": "..."})
    msgs = await bus.get_messages("security-1")
    await bus.drain()
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from cap.db import get_db

logger = logging.getLogger("cap.lib.agent_bus")

# ---------------------------------------------------------------------------
# Message envelope
# ---------------------------------------------------------------------------

@dataclass
class BusMessage:
    """Immutable envelope carried by the bus."""

    id: str
    sender: str          # agent instance id  (e.g. "dev-1")
    sender_type: str     # agent role          (e.g. "dev")
    topic: str
    payload: dict
    timestamp: float
    session_id: str
    reply_to: Optional[str] = None  # message_id this is a direct response to

    # ---- helpers -----------------------------------------------------------

    def to_row(self) -> Tuple:
        """Return a tuple matching the bus_messages INSERT order."""
        return (
            self.id,
            self.session_id,
            self.sender,
            self.sender_type,
            self.topic,
            json.dumps(self.payload),
            self.timestamp,
            self.reply_to,
        )

    @staticmethod
    def from_row(row: sqlite3.Row) -> "BusMessage":
        """Reconstruct from a sqlite3.Row (or dict-like)."""
        return BusMessage(
            id=row["id"],
            sender=row["sender"],
            sender_type=row["sender_type"],
            topic=row["topic"],
            payload=json.loads(row["payload_json"]),
            timestamp=row["timestamp"],
            session_id=row["session_id"],
            reply_to=row["reply_to"],
        )


# ---------------------------------------------------------------------------
# AgentBus
# ---------------------------------------------------------------------------

class AgentBus:
    """Pub/sub message bus for agent-to-agent communication.

    Features
    --------
    - Topic-based publish/subscribe with fnmatch wildcards.
    - Request-response pattern (synchronous query to another agent type).
    - Broadcast to all subscribers.
    - SQLite persistence for audit trail (fire-and-forget writes).
    - Per-agent delivery queues backed by asyncio.Queue.
    """

    # ---- DDL ---------------------------------------------------------------

    _DDL = """
        CREATE TABLE IF NOT EXISTS bus_messages (
            id          TEXT    PRIMARY KEY,
            session_id  TEXT    NOT NULL,
            sender      TEXT    NOT NULL,
            sender_type TEXT    NOT NULL,
            topic       TEXT    NOT NULL,
            payload_json TEXT   NOT NULL,
            timestamp   REAL    NOT NULL,
            reply_to    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_bus_session
            ON bus_messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_bus_topic
            ON bus_messages(topic);
        CREATE INDEX IF NOT EXISTS idx_bus_ts
            ON bus_messages(timestamp);

        CREATE TABLE IF NOT EXISTS bus_subscriptions (
            agent_id       TEXT NOT NULL,
            topic_pattern  TEXT NOT NULL,
            session_id     TEXT NOT NULL,
            created_at     REAL NOT NULL,
            PRIMARY KEY (agent_id, topic_pattern, session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_bus_sub_session
            ON bus_subscriptions(session_id);
    """

    # ---- lifecycle ---------------------------------------------------------

    def __init__(self, session_id: str, db_path: Optional[str] = None) -> None:
        """Initialise the bus for a given session.

        Args:
            session_id: Opaque string that scopes all messages and
                        subscriptions for this conversation/workflow.
            db_path:    Path to the SQLite DB.  Defaults to CAP default
                        (``CAP_HOME/data/platform.db``).
        """
        if not session_id:
            raise ValueError("session_id must be a non-empty string")

        self._session_id = session_id
        self._db_path = db_path

        # agent_id -> set of topic patterns
        self._subscriptions: Dict[str, Set[str]] = {}
        # agent_id -> asyncio.Queue[BusMessage]
        self._queues: Dict[str, asyncio.Queue] = {}
        # pending reply futures: request message_id -> asyncio.Future
        self._pending: Dict[str, asyncio.Future] = {}
        # total messages published this session
        self._message_count: int = 0

        self._ensure_schema()
        self._load_subscriptions()

    # ---- internal helpers --------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        """Open a short-lived DB connection (one per call, closed by caller)."""
        return get_db(self._db_path)

    def _ensure_schema(self) -> None:
        """Create bus tables if they don't exist yet."""
        with self._db() as conn:
            conn.executescript(self._DDL)

    def _load_subscriptions(self) -> None:
        """Restore in-memory subscription registry from persistent store."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT agent_id, topic_pattern FROM bus_subscriptions "
                "WHERE session_id = ?",
                (self._session_id,),
            ).fetchall()
        for row in rows:
            agent_id, pattern = row["agent_id"], row["topic_pattern"]
            self._subscriptions.setdefault(agent_id, set()).add(pattern)
            # pre-create queue so agents that were subscribed in a prior call
            # can immediately call get_messages without subscribing again.
            self.get_delivery_queue(agent_id)

        logger.debug(
            "bus[%s] loaded %d subscription(s) from DB",
            self._session_id,
            len(rows),
        )

    def _persist_message(self, msg: BusMessage) -> None:
        """Write message to SQLite.  Called from asyncio.create_task; never awaited."""
        try:
            with self._db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO bus_messages "
                    "(id, session_id, sender, sender_type, topic, payload_json, timestamp, reply_to) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    msg.to_row(),
                )
        except Exception:
            logger.exception("bus[%s] failed to persist message %s", self._session_id, msg.id)

    def _matching_agents(self, topic: str) -> List[str]:
        """Return agent IDs whose subscribed patterns match *topic*."""
        matched: List[str] = []
        for agent_id, patterns in self._subscriptions.items():
            for pattern in patterns:
                if fnmatch.fnmatchcase(topic, pattern):
                    matched.append(agent_id)
                    break
        return matched

    def _make_message(
        self,
        sender: str,
        sender_type: str,
        topic: str,
        payload: dict,
        reply_to: Optional[str] = None,
    ) -> BusMessage:
        return BusMessage(
            id=str(uuid.uuid4()),
            sender=sender,
            sender_type=sender_type,
            topic=topic,
            payload=payload,
            timestamp=time.time(),
            session_id=self._session_id,
            reply_to=reply_to,
        )

    def _deliver(self, msg: BusMessage) -> None:
        """Dispatch *msg* to all matching subscriber queues."""
        recipients = self._matching_agents(msg.topic)
        for agent_id in recipients:
            q = self.get_delivery_queue(agent_id)
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(
                    "bus[%s] queue full for agent %s; dropping message %s",
                    self._session_id, agent_id, msg.id,
                )

        if not recipients:
            logger.debug(
                "bus[%s] topic %s has no subscribers (msg %s)",
                self._session_id, msg.topic, msg.id,
            )

    # ---- public API — subscriptions ----------------------------------------

    async def subscribe(self, agent_id: str, topic_pattern: str) -> None:
        """Subscribe *agent_id* to all topics matching *topic_pattern*.

        Patterns follow :mod:`fnmatch` rules (``*`` and ``?`` wildcards).
        Subscription is persisted to SQLite so it survives restarts.

        Args:
            agent_id:      Unique identifier for this agent instance.
            topic_pattern: fnmatch pattern, e.g. ``"findings.*"``.
        """
        if not agent_id or not topic_pattern:
            raise ValueError("agent_id and topic_pattern must be non-empty")

        self._subscriptions.setdefault(agent_id, set()).add(topic_pattern)
        self.get_delivery_queue(agent_id)

        with self._db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bus_subscriptions "
                "(agent_id, topic_pattern, session_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (agent_id, topic_pattern, self._session_id, time.time()),
            )

        logger.debug(
            "bus[%s] %s subscribed to '%s'",
            self._session_id, agent_id, topic_pattern,
        )

    async def unsubscribe(self, agent_id: str, topic_pattern: str) -> None:
        """Remove a subscription.

        Args:
            agent_id:      Agent whose subscription to remove.
            topic_pattern: The exact pattern string that was subscribed.
        """
        patterns = self._subscriptions.get(agent_id, set())
        patterns.discard(topic_pattern)

        with self._db() as conn:
            conn.execute(
                "DELETE FROM bus_subscriptions "
                "WHERE agent_id = ? AND topic_pattern = ? AND session_id = ?",
                (agent_id, topic_pattern, self._session_id),
            )

        logger.debug(
            "bus[%s] %s unsubscribed from '%s'",
            self._session_id, agent_id, topic_pattern,
        )

    # ---- public API — pub/sub ----------------------------------------------

    async def publish(
        self,
        sender: str,
        sender_type: str,
        topic: str,
        payload: dict,
    ) -> str:
        """Publish a message to *topic*.

        Args:
            sender:      Agent instance ID of the publisher.
            sender_type: Role/type of the publisher (e.g. ``"dev"``).
            topic:       Dot-separated topic string (e.g. ``"findings.security"``).
            payload:     Arbitrary dict — keep small (findings/summaries, not full output).

        Returns:
            The generated message ID (UUID4 string).
        """
        if not topic:
            raise ValueError("topic must be a non-empty string")

        msg = self._make_message(sender, sender_type, topic, payload)
        self._message_count += 1

        # Resolve pending request futures BEFORE delivering to queues
        self._resolve_pending(msg)

        # Deliver to subscriber queues
        self._deliver(msg)

        # Persist fire-and-forget (don't block the caller on DB I/O)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._persist_message, msg)

        logger.debug(
            "bus[%s] published %s on '%s' from %s",
            self._session_id, msg.id, topic, sender,
        )
        return msg.id

    async def broadcast(
        self,
        sender: str,
        sender_type: str,
        payload: dict,
    ) -> str:
        """Broadcast *payload* to all agents in the session.

        Uses the special topic ``"broadcast"`` which subscribers can filter
        with the pattern ``"broadcast"`` or ``"*"``.

        Args:
            sender:      Agent instance ID of the broadcaster.
            sender_type: Role/type of the broadcaster.
            payload:     Arbitrary dict to broadcast.

        Returns:
            The generated message ID.
        """
        msg = self._make_message(sender, sender_type, "broadcast", payload)
        self._message_count += 1

        # Deliver to ALL registered agents, bypassing pattern matching
        for agent_id in list(self._queues):
            q = self._queues[agent_id]
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning(
                    "bus[%s] queue full for agent %s during broadcast %s",
                    self._session_id, agent_id, msg.id,
                )

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._persist_message, msg)

        logger.debug(
            "bus[%s] broadcast %s from %s to %d agents",
            self._session_id, msg.id, sender, len(self._queues),
        )
        return msg.id

    # ---- public API — request / response -----------------------------------

    async def request(
        self,
        sender: str,
        sender_type: str,
        target_agent_type: str,
        query: str,
        timeout: float = 30.0,
    ) -> Optional[dict]:
        """Send a request to agents subscribed to ``request.<target_agent_type>``.

        Blocks until a matching response arrives or *timeout* elapses.

        Args:
            sender:            Requesting agent's instance ID.
            sender_type:       Requesting agent's role.
            target_agent_type: Role of the agent expected to answer
                               (e.g. ``"security"``).
            query:             Natural-language or structured query string.
            timeout:           Seconds to wait for a response.

        Returns:
            The response payload dict, or ``None`` on timeout.
        """
        topic = f"request.{target_agent_type}"
        payload = {"query": query, "requester": sender}
        msg = self._make_message(sender, sender_type, topic, payload)
        self._message_count += 1

        # Register future before delivering so responder can't race past us
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg.id] = fut

        self._deliver(msg)
        loop.run_in_executor(None, self._persist_message, msg)

        logger.debug(
            "bus[%s] request %s → %s (timeout=%.1fs)",
            self._session_id, msg.id, target_agent_type, timeout,
        )

        try:
            response_payload: dict = await asyncio.wait_for(
                asyncio.shield(fut), timeout=timeout
            )
            return response_payload
        except asyncio.TimeoutError:
            logger.warning(
                "bus[%s] request %s to '%s' timed out after %.1fs",
                self._session_id, msg.id, target_agent_type, timeout,
            )
            return None
        finally:
            self._pending.pop(msg.id, None)

    async def respond(
        self,
        responder: str,
        responder_type: str,
        request_message_id: str,
        response: dict,
    ) -> str:
        """Publish a response to a previously received request.

        Args:
            responder:          Responding agent's instance ID.
            responder_type:     Responding agent's role.
            request_message_id: The ``id`` of the :class:`BusMessage` being
                                responded to.
            response:           Response payload dict.

        Returns:
            The generated response message ID.
        """
        if not request_message_id:
            raise ValueError("request_message_id must be non-empty")

        topic = f"response.{responder_type}"
        msg = self._make_message(
            responder, responder_type, topic, response, reply_to=request_message_id
        )
        self._message_count += 1

        # Resolve the waiting future, if present
        self._resolve_pending(msg)

        self._deliver(msg)

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._persist_message, msg)

        logger.debug(
            "bus[%s] response %s from %s for request %s",
            self._session_id, msg.id, responder, request_message_id,
        )
        return msg.id

    def _resolve_pending(self, msg: BusMessage) -> None:
        """If *msg* is a reply to a pending future, resolve it."""
        if msg.reply_to and msg.reply_to in self._pending:
            fut = self._pending[msg.reply_to]
            if not fut.done():
                fut.set_result(msg.payload)

    # ---- public API — delivery ---------------------------------------------

    async def get_messages(
        self,
        agent_id: str,
        timeout: float = 0.0,
    ) -> List[BusMessage]:
        """Drain all pending messages for *agent_id*.

        Args:
            agent_id: The agent whose queue to drain.
            timeout:  If > 0, wait up to this many seconds for the *first*
                      message before giving up.  Subsequent messages in the
                      same call are always read non-blocking.

        Returns:
            List of :class:`BusMessage` objects (may be empty).
        """
        q = self.get_delivery_queue(agent_id)
        messages: List[BusMessage] = []

        if timeout > 0.0 and q.empty():
            try:
                first = await asyncio.wait_for(q.get(), timeout=timeout)
                messages.append(first)
                q.task_done()
            except asyncio.TimeoutError:
                return messages

        # Drain remainder non-blocking
        while not q.empty():
            try:
                msg = q.get_nowait()
                messages.append(msg)
                q.task_done()
            except asyncio.QueueEmpty:
                break

        return messages

    def get_delivery_queue(self, agent_id: str) -> asyncio.Queue:
        """Get or create the delivery queue for *agent_id*.

        The queue is unbounded to avoid silently dropping messages by default.
        Callers that need back-pressure should drain frequently.

        Args:
            agent_id: The agent whose queue to retrieve.

        Returns:
            The :class:`asyncio.Queue` for this agent.
        """
        if agent_id not in self._queues:
            self._queues[agent_id] = asyncio.Queue()
        return self._queues[agent_id]

    # ---- public API — history ----------------------------------------------

    async def get_topic_history(
        self,
        topic_pattern: str,
        limit: int = 50,
    ) -> List[BusMessage]:
        """Retrieve historical messages matching *topic_pattern* from SQLite.

        Args:
            topic_pattern: fnmatch pattern (e.g. ``"findings.*"``).
            limit:         Maximum number of messages to return (newest first).

        Returns:
            List of :class:`BusMessage` ordered by timestamp descending.
        """
        if limit <= 0:
            raise ValueError("limit must be a positive integer")

        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM bus_messages "
                "WHERE session_id = ? "
                "ORDER BY timestamp DESC "
                "LIMIT ?",
                (self._session_id, limit),
            ).fetchall()

        results: List[BusMessage] = []
        for row in rows:
            msg = BusMessage.from_row(row)
            if fnmatch.fnmatchcase(msg.topic, topic_pattern):
                results.append(msg)

        return results

    # ---- public API — lifecycle --------------------------------------------

    async def drain(self) -> None:
        """Flush all in-memory queues and cancel pending request futures.

        Call this when the session ends to release resources cleanly.
        """
        # Cancel all pending request futures
        for msg_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
                logger.debug("bus[%s] cancelled pending request future %s", self._session_id, msg_id)
        self._pending.clear()

        # Join all delivery queues
        for agent_id, q in list(self._queues.items()):
            remaining = q.qsize()
            if remaining:
                logger.debug(
                    "bus[%s] draining %d unread message(s) from agent %s",
                    self._session_id, remaining, agent_id,
                )
            # Mark all queued items as done so join() doesn't hang
            while not q.empty():
                try:
                    q.get_nowait()
                    q.task_done()
                except asyncio.QueueEmpty:
                    break

        logger.info(
            "bus[%s] drained — %d total message(s) published",
            self._session_id, self._message_count,
        )

    # ---- properties --------------------------------------------------------

    @property
    def message_count(self) -> int:
        """Total messages published in this session (includes broadcasts and responses)."""
        return self._message_count

    @property
    def session_id(self) -> str:
        """The session ID this bus was created for."""
        return self._session_id

"""Per-agent isolated context with shared state access.

Each agent execution gets an AgentContext that provides:
- Isolated local state (private to the agent)
- Access to a thread-safe SharedState store (accessible by all agents in a session)
- An inbox (asyncio.Queue) for receiving messages from other agents
- Methods to publish findings and request info from other agents

SharedState is write-through: in-memory dict for fast reads, SQLite for
durability across restarts. Pattern-based subscriptions use fnmatch.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cap.db import get_db, migrate

logger = logging.getLogger("cap.lib.agent_context")

# ---------------------------------------------------------------------------
# DDL — registered in migrate() pattern but also run lazily on first use.
# ---------------------------------------------------------------------------

_SHARED_STATE_DDL = """
CREATE TABLE IF NOT EXISTS shared_state (
    session_id  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value_json  TEXT    NOT NULL,
    publisher   TEXT    NOT NULL DEFAULT 'system',
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (session_id, key)
);

CREATE INDEX IF NOT EXISTS idx_shared_state_session
    ON shared_state(session_id);
"""


def _ensure_table(db_path: Optional[str] = None) -> None:
    """Create shared_state table if it does not exist yet.

    Called lazily so that importing this module never touches the filesystem
    until a SharedState is actually instantiated.
    """
    db = get_db(db_path)
    try:
        db.executescript(_SHARED_STATE_DDL)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SharedState
# ---------------------------------------------------------------------------


class SharedState:
    """Thread-safe shared state store accessible by all agents in a session.

    Backed by SQLite for persistence across restarts. In-memory cache with
    write-through to the DB.  All mutations are serialised through an
    asyncio.Lock so concurrent coroutines cannot interleave writes.

    Subscribers register fnmatch patterns; any matching set() call will
    invoke their callback with (key, value).
    """

    def __init__(self, session_id: str, db_path: Optional[str] = None) -> None:
        """Initialise SharedState for *session_id*.

        Args:
            session_id: Unique identifier for the agent session.
            db_path: Override the default SQLite path (~/.cap/cap.db).
        """
        if not session_id:
            raise ValueError("session_id must be a non-empty string")

        self._session_id = session_id
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] = {}
        self._subscribers: list[tuple[str, Callable[[str, Any], Any]]] = []

        # Ensure table exists before loading.
        _ensure_table(db_path)
        self._cache = self._load_from_db()

        logger.debug(
            "SharedState initialised for session=%s, %d keys loaded",
            session_id,
            len(self._cache),
        )

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Any:
        """Return the value for *key*, or ``None`` if absent.

        Args:
            key: State key to retrieve.

        Returns:
            Deserialised value or ``None``.
        """
        async with self._lock:
            return self._cache.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        publisher: str = "system",
    ) -> None:
        """Set *key* to *value* and persist to SQLite.

        After writing, all subscribers whose pattern matches *key* are
        notified via their callback (called without holding the lock so
        that callbacks may call back into SharedState without deadlocking).

        Args:
            key: State key to set.
            value: JSON-serialisable value.
            publisher: Identifier of the agent/system setting this value.
        """
        if not key:
            raise ValueError("key must be a non-empty string")

        async with self._lock:
            self._cache[key] = value
            self._persist(key, value, publisher)

        # Notify subscribers outside the lock.
        await self._notify_subscribers(key, value)

    async def get_all(self) -> dict[str, Any]:
        """Return a shallow copy of the entire in-memory cache.

        Returns:
            Dict mapping all keys to their current values.
        """
        async with self._lock:
            return dict(self._cache)

    async def delete(self, key: str) -> None:
        """Remove *key* from shared state and from the DB.

        Args:
            key: State key to delete. No-op if the key does not exist.
        """
        async with self._lock:
            self._cache.pop(key, None)
            db = get_db(self._db_path)
            try:
                db.execute(
                    "DELETE FROM shared_state WHERE session_id = ? AND key = ?",
                    (self._session_id, key),
                )
                db.commit()
            finally:
                db.close()

        logger.debug("SharedState.delete session=%s key=%s", self._session_id, key)

    async def subscribe(
        self,
        pattern: str,
        callback: Callable[[str, Any], Any],
    ) -> None:
        """Register a callback for keys matching an fnmatch *pattern*.

        The callback receives ``(key, value)`` and may be a coroutine function
        or a plain function. Callbacks that raise are logged and suppressed so
        that one bad subscriber cannot break the publisher.

        Args:
            pattern: fnmatch pattern (e.g. ``"findings.*"``, ``"*"``).
            callback: Callable invoked with (key, value) on matching set().
        """
        if not pattern:
            raise ValueError("pattern must be a non-empty string")
        if not callable(callback):
            raise TypeError("callback must be callable")

        async with self._lock:
            self._subscribers.append((pattern, callback))

        logger.debug(
            "SharedState.subscribe session=%s pattern=%s",
            self._session_id,
            pattern,
        )

    async def keys(self, prefix: str = "") -> list[str]:
        """Return sorted list of all keys, optionally filtered by *prefix*.

        Args:
            prefix: Only return keys that start with this string.

        Returns:
            Sorted list of matching key strings.
        """
        async with self._lock:
            if prefix:
                return sorted(k for k in self._cache if k.startswith(prefix))
            return sorted(self._cache.keys())

    async def clear(self) -> None:
        """Remove all keys for this session from memory and from the DB."""
        async with self._lock:
            self._cache.clear()
            db = get_db(self._db_path)
            try:
                db.execute(
                    "DELETE FROM shared_state WHERE session_id = ?",
                    (self._session_id,),
                )
                db.commit()
            finally:
                db.close()

        logger.debug("SharedState.clear session=%s", self._session_id)

    # ------------------------------------------------------------------
    # Persistence helpers (synchronous — called while holding the lock)
    # ------------------------------------------------------------------

    def _persist(self, key: str, value: Any, publisher: str = "system") -> None:
        """Write-through: upsert a single key into SQLite.

        Args:
            key: State key.
            value: Value to serialise as JSON.
            publisher: Identifier of the writing agent.

        Raises:
            TypeError: If *value* is not JSON-serialisable.
        """
        try:
            value_json = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"SharedState value for key '{key}' must be JSON-serialisable: {exc}"
            ) from exc

        db = get_db(self._db_path)
        try:
            db.execute(
                """
                INSERT INTO shared_state (session_id, key, value_json, publisher, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    publisher  = excluded.publisher,
                    updated_at = excluded.updated_at
                """,
                (self._session_id, key, value_json, publisher, time.time()),
            )
            db.commit()
        finally:
            db.close()

    def _load_from_db(self) -> dict[str, Any]:
        """Load all persisted keys for this session from SQLite.

        Returns:
            Dict of key → deserialised value for all rows belonging to
            ``self._session_id``.
        """
        db = get_db(self._db_path)
        try:
            rows = db.execute(
                "SELECT key, value_json FROM shared_state WHERE session_id = ?",
                (self._session_id,),
            ).fetchall()
        finally:
            db.close()

        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                logger.warning(
                    "SharedState: corrupt JSON for session=%s key=%s — skipped",
                    self._session_id,
                    row["key"],
                )
        return result

    # ------------------------------------------------------------------
    # Internal subscription dispatch
    # ------------------------------------------------------------------

    async def _notify_subscribers(self, key: str, value: Any) -> None:
        """Invoke all subscribers whose pattern matches *key*.

        Coroutine callbacks are awaited. Plain callbacks are called
        synchronously. All exceptions are caught and logged.

        Args:
            key: The key that was just set.
            value: The new value.
        """
        async with self._lock:
            matching = [
                cb for pattern, cb in self._subscribers if fnmatch.fnmatch(key, pattern)
            ]

        for callback in matching:
            try:
                result = callback(key, value)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "SharedState subscriber raised for key=%s callback=%s",
                    key,
                    callback,
                )


# ---------------------------------------------------------------------------
# AgentContext
# ---------------------------------------------------------------------------


@dataclass
class AgentContext:
    """Isolated execution context for a single agent invocation.

    Each agent gets its own ``local_state`` dict and ``inbox`` queue.
    All agents in the same session share access to the same ``shared_state``
    instance.

    Attributes:
        agent_id: Unique identifier for this agent invocation.
        agent_type: Category/type label (e.g. "dev", "security").
        task: Human-readable description of the task assigned to this agent.
        workspace: Absolute path to the working directory for this agent.
        session_id: Session to which this context belongs.
        shared_state: Reference to the session-wide SharedState.
        inbox: asyncio.Queue for receiving messages from other agents.
        local_state: Private mutable dict — not visible to other agents.
        messages: Internal message history for this agent (summaries only,
            not full conversation turns).
    """

    # Required fields — no defaults (must come first).
    agent_id: str
    agent_type: str
    task: str
    workspace: str
    session_id: str
    shared_state: SharedState = field(repr=False)
    inbox: asyncio.Queue = field(repr=False)
    # Fields with defaults — must follow all required fields.
    local_state: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    async def publish(self, topic: str, payload: dict) -> None:
        """Publish a finding to the shared message bus.

        The finding is stored in shared state under the key
        ``f"findings.{topic}.{self.agent_id}"`` so that any subscriber
        using a matching pattern will be notified.

        Args:
            topic: Logical topic name (e.g. "security", "cost_estimate").
            payload: JSON-serialisable dict describing the finding.
        """
        if not topic:
            raise ValueError("topic must be a non-empty string")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")

        key = f"findings.{topic}.{self.agent_id}"
        envelope = {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "topic": topic,
            "payload": payload,
            "published_at": time.time(),
        }
        await self.shared_state.set(key, envelope, publisher=self.agent_id)

        logger.debug(
            "AgentContext.publish agent=%s topic=%s key=%s",
            self.agent_id,
            topic,
            key,
        )

    async def get_shared(self, key: str) -> Any:
        """Get a value from shared state.

        Args:
            key: State key to retrieve.

        Returns:
            Deserialised value or ``None`` if absent.
        """
        return await self.shared_state.get(key)

    async def set_shared(self, key: str, value: Any) -> None:
        """Set a value in shared state, attributed to this agent.

        Args:
            key: State key to set.
            value: JSON-serialisable value.
        """
        await self.shared_state.set(key, value, publisher=self.agent_id)

    async def receive(self, timeout: float = 5.0) -> Optional[dict]:
        """Receive a message from the inbox with a timeout.

        Args:
            timeout: Seconds to wait before returning ``None``.

        Returns:
            A message dict, or ``None`` if no message arrived within
            *timeout* seconds.
        """
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def send_to(self, target_inbox: asyncio.Queue, message: dict) -> None:
        """Send a message directly to another agent's inbox.

        The message is stamped with ``sender_id`` and ``sent_at`` before
        being enqueued.

        Args:
            target_inbox: The recipient agent's ``inbox`` queue.
            message: Payload dict. Must be a dict.
        """
        if not isinstance(message, dict):
            raise TypeError("message must be a dict")

        envelope = {
            **message,
            "sender_id": self.agent_id,
            "sent_at": time.time(),
        }
        await target_inbox.put(envelope)

        logger.debug(
            "AgentContext.send_to agent=%s → queue (size=%d)",
            self.agent_id,
            target_inbox.qsize(),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_agent_context(
    agent_id: str,
    agent_type: str,
    task: str,
    workspace: str,
    session_id: str,
    shared_state: SharedState,
    bus: Optional[asyncio.Queue] = None,
) -> AgentContext:
    """Create and return a fully initialised AgentContext.

    Args:
        agent_id: Unique identifier for this agent invocation.
        agent_type: Category/type label (e.g. "dev", "security").
        task: Human-readable description of the task.
        workspace: Absolute path to the working directory.
        session_id: Session identifier shared across agents.
        shared_state: Pre-constructed SharedState for the session.
        bus: Optional pre-existing asyncio.Queue to use as the inbox.
            A new queue is created if not provided.

    Returns:
        A new AgentContext ready for use.

    Raises:
        ValueError: If any required string argument is empty.
        TypeError: If *shared_state* is not a SharedState instance.
    """
    for name, val in [
        ("agent_id", agent_id),
        ("agent_type", agent_type),
        ("task", task),
        ("workspace", workspace),
        ("session_id", session_id),
    ]:
        if not val or not isinstance(val, str):
            raise ValueError(f"{name} must be a non-empty string")

    if not isinstance(shared_state, SharedState):
        raise TypeError(
            f"shared_state must be a SharedState instance, got {type(shared_state).__name__}"
        )

    inbox: asyncio.Queue = bus if bus is not None else asyncio.Queue()

    ctx = AgentContext(
        agent_id=agent_id,
        agent_type=agent_type,
        task=task,
        workspace=workspace,
        session_id=session_id,
        local_state={},
        messages=[],
        shared_state=shared_state,
        inbox=inbox,
    )

    logger.debug(
        "create_agent_context agent_id=%s type=%s session=%s",
        agent_id,
        agent_type,
        session_id,
    )
    return ctx

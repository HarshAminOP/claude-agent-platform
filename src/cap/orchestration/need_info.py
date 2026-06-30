"""
NEED_INFO Protocol for CAP inter-agent communication.

When an agent cannot proceed without additional information, it emits a
NEED_INFO signal. The orchestrator resolves it through a 4-step cascade:
  1. Check memory (active memory search)
  2. Check context thread (other agents' outputs)
  3. Non-blocking assumption (proceed with stated assumption, log it)
  4. PO escalation (blocking=True and no resolution found)

Reference: CAP System Design Section 9 — NEED_INFO Protocol.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .context import ContextThread

logger = logging.getLogger(__name__)

# Marker used by agents to signal NEED_INFO in their output
NEED_INFO_MARKER = "---NEED_INFO---"


class NeedInfoEscalation(Exception):
    """Raised when a blocking NEED_INFO cannot be resolved automatically."""

    def __init__(self, question: str, context: str, options: list[str] = None):
        self.question = question
        self.context = context
        self.options = options or []
        super().__init__(f"Blocking NEED_INFO: {question}")


@dataclass
class NeedInfo:
    """Represents an agent's request for additional information."""

    agent_type: str
    question: str
    context: str  # why this info is needed
    blocking: bool = True  # True = cannot proceed, False = can proceed with assumption
    assumption: Optional[str] = None  # what agent will assume if non-blocking
    options: list[str] = field(default_factory=list)  # suggested answers

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "question": self.question,
            "context": self.context,
            "blocking": self.blocking,
            "assumption": self.assumption,
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NeedInfo":
        return cls(
            agent_type=data["agent_type"],
            question=data["question"],
            context=data.get("context", ""),
            blocking=data.get("blocking", True),
            assumption=data.get("assumption"),
            options=data.get("options", []),
        )


@dataclass
class Resolution:
    """Result of resolving a NEED_INFO request."""

    answer: str
    source: str  # memory, thread, assumption, escalation
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "source": self.source,
            "confidence": self.confidence,
        }


def parse_need_info_from_output(agent_type: str, output: str) -> Optional[NeedInfo]:
    """
    Parse a NEED_INFO block from agent output text.

    Expected format in agent output:
        ---NEED_INFO---
        question: <question text>
        context: <why needed>
        blocking: true|false
        assumption: <fallback assumption if non-blocking>
        options: option1 | option2 | option3
        ---NEED_INFO---

    Returns NeedInfo if found, None otherwise.
    """
    if NEED_INFO_MARKER not in output:
        return None

    # Extract content between markers
    pattern = re.compile(
        re.escape(NEED_INFO_MARKER) + r"\s*\n(.*?)\n\s*" + re.escape(NEED_INFO_MARKER),
        re.DOTALL,
    )
    match = pattern.search(output)
    if not match:
        return None

    block = match.group(1).strip()
    fields: dict[str, str] = {}
    for line in block.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()

    question = fields.get("question", "")
    if not question:
        return None

    blocking_str = fields.get("blocking", "true").lower()
    blocking = blocking_str not in ("false", "no", "0")

    options_str = fields.get("options", "")
    options = [o.strip() for o in options_str.split("|") if o.strip()] if options_str else []

    return NeedInfo(
        agent_type=agent_type,
        question=question,
        context=fields.get("context", ""),
        blocking=blocking,
        assumption=fields.get("assumption") or None,
        options=options,
    )


def _search_memory(question: str, db: sqlite3.Connection) -> Optional[Resolution]:
    """
    Step 1: Search active memory for an answer to the question.

    Looks in memory_active for entries matching the question keywords.
    Searches per-keyword (words with len > 3) and scores by match count.
    Returns a Resolution if a high-confidence match is found.
    """
    try:
        # Extract meaningful keywords (>3 chars, skip stop words)
        stop_words = {"what", "which", "where", "when", "that", "this", "from", "with", "have", "does"}
        keywords = [
            w for w in question.lower().split()
            if len(w) > 3 and w not in stop_words
        ][:6]

        if not keywords:
            return None

        # Build query: match any keyword, order by importance
        conditions = " OR ".join(["content LIKE ?"] * len(keywords))
        params = [f"%{kw}%" for kw in keywords]

        rows = db.execute(
            f"""SELECT content, importance FROM memory_active
               WHERE ({conditions})
               AND consolidated_into IS NULL
               ORDER BY importance DESC, last_accessed DESC
               LIMIT 5""",
            params,
        ).fetchall()

        if not rows:
            return None

        # Score each row by how many keywords it matches
        best_content = None
        best_score = 0.0
        for row in rows:
            content = row[0] if not isinstance(row, sqlite3.Row) else row["content"]
            importance = row[1] if not isinstance(row, sqlite3.Row) else row["importance"]
            content_lower = content.lower()
            matches = sum(1 for kw in keywords if kw in content_lower)
            score = (matches / len(keywords)) * (importance or 0.5)
            if score > best_score:
                best_score = score
                best_content = content

        # Require at least 30% keyword match weighted by importance
        if best_score > 0.3 and best_content:
            return Resolution(
                answer=best_content,
                source="memory",
                confidence=min(1.0, best_score),
            )

    except (sqlite3.OperationalError, KeyError):
        pass

    return None


def _search_thread(question: str, thread: ContextThread) -> Optional[Resolution]:
    """
    Step 2: Search prior agent outputs in the context thread for the answer.

    Checks completed frames' outputs for keyword overlap with the question.
    """
    question_lower = question.lower()
    question_words = set(question_lower.split())

    for frame in thread.frames:
        if frame.status != "completed":
            continue
        if not frame.outputs:
            continue

        combined_output = " ".join(frame.outputs).lower()
        # Check for keyword overlap
        overlap = sum(1 for w in question_words if w in combined_output and len(w) > 3)
        if overlap >= 2:
            # Found a relevant frame; extract the answer
            answer_text = " ".join(frame.outputs)
            if len(answer_text) > 500:
                answer_text = answer_text[:500]
            return Resolution(
                answer=answer_text,
                source="thread",
                confidence=min(1.0, overlap * 0.2),
            )

    return None


def handle_need_info(
    need_info: NeedInfo,
    context_thread: ContextThread,
    db: sqlite3.Connection,
) -> Resolution:
    """
    Resolve a NEED_INFO request through the 4-step cascade.

    Resolution order:
      1. Check memory for the answer
      2. Check other agents' outputs in the context thread
      3. If non-blocking, use the assumption and log it
      4. If blocking, escalate to user (PO) via NeedInfoEscalation

    Args:
        need_info: The NeedInfo request from the agent.
        context_thread: Current orchestration's context thread.
        db: SQLite connection with CAP schema.

    Returns:
        Resolution with the answer and its source.

    Raises:
        NeedInfoEscalation: If blocking=True and no resolution found.
    """
    logger.info(
        "Handling NEED_INFO from %s: %s (blocking=%s)",
        need_info.agent_type,
        need_info.question,
        need_info.blocking,
    )

    # Step 1: Check memory
    resolution = _search_memory(need_info.question, db)
    if resolution:
        logger.info("NEED_INFO resolved from memory (confidence=%.2f)", resolution.confidence)
        return resolution

    # Step 2: Check thread outputs
    resolution = _search_thread(need_info.question, context_thread)
    if resolution:
        logger.info("NEED_INFO resolved from thread (confidence=%.2f)", resolution.confidence)
        return resolution

    # Step 3: Non-blocking assumption
    if not need_info.blocking and need_info.assumption:
        logger.info("NEED_INFO using assumption: %s", need_info.assumption)
        _log_assumption(need_info, context_thread, db)
        return Resolution(
            answer=need_info.assumption,
            source="assumption",
            confidence=0.3,
        )

    # Step 4: Blocking escalation
    logger.warning(
        "NEED_INFO escalation required: %s (agent=%s)",
        need_info.question,
        need_info.agent_type,
    )
    raise NeedInfoEscalation(
        question=need_info.question,
        context=need_info.context,
        options=need_info.options,
    )


def _log_assumption(
    need_info: NeedInfo,
    thread: ContextThread,
    db: sqlite3.Connection,
) -> None:
    """Log an assumption for audit trail via the learning engine."""
    try:
        from cap.learning.engine import record_routing

        record_routing(
            decision={
                "session_id": thread.orchestration_id,
                "task_description": f"ASSUMPTION by {need_info.agent_type}: {need_info.assumption}",
                "complexity_score": 0.0,
                "tier_selected": "inline",
                "agents_used": [need_info.agent_type],
            },
            db=db,
        )
    except (ImportError, sqlite3.OperationalError, Exception) as e:
        logger.debug("Could not log assumption: %s", e)


def format_need_info_for_user(need_info: NeedInfo) -> str:
    """
    Format a NEED_INFO escalation as a user-facing message.

    Used when the orchestrator needs to surface the question to the PO.
    """
    lines = [
        f"Agent '{need_info.agent_type}' needs information to proceed:",
        f"",
        f"  Question: {need_info.question}",
        f"  Context:  {need_info.context}",
    ]
    if need_info.options:
        lines.append(f"  Options:")
        for i, opt in enumerate(need_info.options, 1):
            lines.append(f"    {i}. {opt}")
    return "\n".join(lines)

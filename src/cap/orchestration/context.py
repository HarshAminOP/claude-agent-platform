"""
Context threading for inter-agent communication in CAP orchestration.

Provides:
- ContextFrame: represents one agent's execution context and outputs.
- ContextThread: ordered list of frames with summarization for next agent.

When the thread exceeds the token budget, older frames are summarized
to keep context compact while preserving critical information.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ContextFrame:
    """A single agent's execution context within an orchestration."""

    agent_type: str
    task: str
    status: str = "pending"  # pending, running, completed, failed
    outputs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # scratchpad keys
    constraints: list[str] = field(default_factory=list)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    token_count: int = 0

    def mark_running(self) -> None:
        self.status = "running"
        self.started_at = time.time()

    def mark_completed(self, outputs: list[str] = None, artifacts: list[str] = None) -> None:
        self.status = "completed"
        self.completed_at = time.time()
        if outputs:
            self.outputs = outputs
        if artifacts:
            self.artifacts = artifacts

    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.completed_at = time.time()
        self.outputs = [f"FAILED: {reason}"]

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "task": self.task,
            "status": self.status,
            "outputs": self.outputs,
            "artifacts": self.artifacts,
            "constraints": self.constraints,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContextFrame":
        return cls(
            agent_type=data["agent_type"],
            task=data["task"],
            status=data.get("status", "pending"),
            outputs=data.get("outputs", []),
            artifacts=data.get("artifacts", []),
            constraints=data.get("constraints", []),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            token_count=data.get("token_count", 0),
        )


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return len(text) // 4


def _summarize_frame(frame: ContextFrame) -> dict:
    """Create a compact summary of a frame for context passing."""
    output_summary = ""
    if frame.outputs:
        # Take first 200 chars of combined outputs
        combined = " | ".join(frame.outputs)
        output_summary = combined[:200]

    return {
        "agent": frame.agent_type,
        "task": frame.task[:150],
        "status": frame.status,
        "output_summary": output_summary,
        "artifacts": frame.artifacts,
    }


class ContextThread:
    """
    Ordered sequence of ContextFrames representing a multi-agent orchestration.

    Supports:
    - Adding frames as agents are dispatched
    - Generating compact summaries for the next agent
    - Truncation when thread exceeds token budget
    """

    def __init__(self, orchestration_id: str = ""):
        self.orchestration_id = orchestration_id
        self.frames: list[ContextFrame] = []
        self.created_at: float = time.time()

    def add_frame(self, frame: ContextFrame) -> None:
        """Add a frame to the thread."""
        self.frames.append(frame)

    def get_summary_for_next_agent(self, max_tokens: int = 2000) -> list[dict]:
        """
        Generate a summary of prior frames for the next agent's context.

        Prioritizes recent frames. If total exceeds max_tokens,
        older frames are progressively summarized (truncated).

        Args:
            max_tokens: Maximum token budget for the summary.

        Returns:
            List of summary dicts, most recent last.
        """
        summaries = []
        token_budget = max_tokens

        # Process frames from most recent to oldest
        for frame in reversed(self.frames):
            # Skip pending frames and orchestrator frames
            if frame.status == "pending":
                continue
            if frame.agent_type == "orchestrator":
                continue

            summary = _summarize_frame(frame)
            summary_text = json.dumps(summary)
            est_tokens = _estimate_tokens(summary_text)

            if est_tokens > token_budget:
                # If we can't fit even a truncated version, stop
                if token_budget < 50:
                    break
                # Truncate the summary to fit
                summary["output_summary"] = summary["output_summary"][: token_budget * 2]
                summary["task"] = summary["task"][:80]
                summaries.append(summary)
                break
            else:
                summaries.append(summary)
                token_budget -= est_tokens

        # Return in chronological order
        return list(reversed(summaries))

    def get_completed_frames(self) -> list[ContextFrame]:
        """Return all completed frames."""
        return [f for f in self.frames if f.status == "completed"]

    def get_failed_frames(self) -> list[ContextFrame]:
        """Return all failed frames."""
        return [f for f in self.frames if f.status == "failed"]

    def total_token_count(self) -> int:
        """Sum of all frame token counts."""
        return sum(f.token_count for f in self.frames)

    def to_dict(self) -> dict:
        return {
            "orchestration_id": self.orchestration_id,
            "created_at": self.created_at,
            "frames": [f.to_dict() for f in self.frames],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContextThread":
        thread = cls(orchestration_id=data.get("orchestration_id", ""))
        thread.created_at = data.get("created_at", time.time())
        for frame_data in data.get("frames", []):
            thread.add_frame(ContextFrame.from_dict(frame_data))
        return thread

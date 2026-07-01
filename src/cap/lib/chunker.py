"""Content chunking module for CAP knowledge ingestion.

Provides deterministic, bounded splitting of text/code content into Chunk
objects suitable for embedding and retrieval.  Every produced chunk is
guaranteed to be <= max_size characters.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ChunkStrategy(Enum):
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    CHARACTER = "character"


@dataclass
class Chunk:
    text: str
    index: int
    start_pos: int
    end_pos: int
    content_hash: str  # SHA-256 hex, first 32 chars
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Allow callers to pass an empty hash and have it computed here,
        # but normally the factory sets it directly.
        if not self.content_hash:
            self.content_hash = _hash(self.text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARIES = re.compile(r"(?<=[\.\!\?])\s+|\n\n")
_PARAGRAPH_BOUNDARY = re.compile(r"\n\n+")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _make_chunk(text: str, index: int, start_pos: int) -> Chunk:
    return Chunk(
        text=text,
        index=index,
        start_pos=start_pos,
        end_pos=start_pos + len(text),
        content_hash=_hash(text),
    )


# ---------------------------------------------------------------------------
# CHARACTER strategy (the guaranteed-bounded fallback)
# ---------------------------------------------------------------------------


def _character_chunks(
    content: str,
    max_size: int,
    overlap: int,
    index_offset: int = 0,
    pos_offset: int = 0,
) -> list[Chunk]:
    """Split *content* into fixed-size windows with *overlap* chars of context.

    This is the only strategy that has a hard size guarantee.  All other
    strategies use it as a fallback for any oversized segment.
    """
    if not content:
        return []

    chunks: list[Chunk] = []
    step = max(1, max_size - overlap)
    pos = 0
    idx = index_offset

    while pos < len(content):
        end = min(pos + max_size, len(content))
        text = content[pos:end]
        chunks.append(_make_chunk(text, idx, pos_offset + pos))
        idx += 1
        pos += step

    return chunks


# ---------------------------------------------------------------------------
# SENTENCE strategy
# ---------------------------------------------------------------------------


def _sentence_chunks(
    content: str,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """Split on sentence/paragraph boundaries; fall back to CHARACTER for long sentences."""
    # Split into raw sentence candidates
    parts: list[str] = []
    last = 0
    for m in _SENTENCE_BOUNDARIES.finditer(content):
        segment = content[last : m.end()]
        if segment:
            parts.append(segment)
        last = m.end()
    tail = content[last:]
    if tail:
        parts.append(tail)

    chunks: list[Chunk] = []
    buffer = ""
    buffer_start = 0  # absolute position in *content*
    cursor = 0        # tracks position as we iterate parts

    def flush(buf: str, start: int) -> None:
        if not buf:
            return
        if len(buf) > max_size:
            # CHARACTER fallback for any oversized buffer
            sub = _character_chunks(buf, max_size, overlap, len(chunks), start)
            chunks.extend(sub)
        else:
            chunks.append(_make_chunk(buf, len(chunks), start))

    for part in parts:
        part_start = cursor
        cursor += len(part)

        if len(buffer) + len(part) <= max_size:
            if not buffer:
                buffer_start = part_start
            buffer += part
        else:
            flush(buffer, buffer_start)
            # overlap: carry the tail of the flushed buffer
            if overlap > 0 and buffer:
                carry = buffer[-overlap:]
                carry_start = buffer_start + len(buffer) - len(carry)
                buffer = carry + part
                buffer_start = carry_start
            else:
                buffer = part
                buffer_start = part_start

    flush(buffer, buffer_start)
    return chunks


# ---------------------------------------------------------------------------
# PARAGRAPH strategy
# ---------------------------------------------------------------------------


def _paragraph_chunks(
    content: str,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """Split on blank-line boundaries; fall back to CHARACTER for long paragraphs."""
    raw_paragraphs: list[tuple[str, int]] = []  # (text, start_pos)
    last = 0
    for m in _PARAGRAPH_BOUNDARY.finditer(content):
        para = content[last : m.start()]
        if para.strip():
            raw_paragraphs.append((para, last))
        last = m.end()
    tail = content[last:]
    if tail.strip():
        raw_paragraphs.append((tail, last))

    chunks: list[Chunk] = []
    buffer = ""
    buffer_start = 0

    def flush(buf: str, start: int) -> None:
        if not buf:
            return
        if len(buf) > max_size:
            sub = _character_chunks(buf, max_size, overlap, len(chunks), start)
            chunks.extend(sub)
        else:
            chunks.append(_make_chunk(buf, len(chunks), start))

    for para, para_start in raw_paragraphs:
        separator = "\n\n" if buffer else ""
        candidate = buffer + separator + para

        if len(candidate) <= max_size:
            if not buffer:
                buffer_start = para_start
            buffer = candidate
        else:
            flush(buffer, buffer_start)
            if overlap > 0 and buffer:
                carry = buffer[-overlap:]
                carry_start = buffer_start + len(buffer) - len(carry)
                buffer = carry + "\n\n" + para
                buffer_start = carry_start
            else:
                buffer = para
                buffer_start = para_start

    flush(buffer, buffer_start)
    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_content(
    content: str,
    strategy: ChunkStrategy = ChunkStrategy.SENTENCE,
    max_size: int = 1024,
    overlap: int = 100,
) -> list[Chunk]:
    """Split *content* into a list of :class:`Chunk` objects.

    Parameters
    ----------
    content:
        Raw text to split.
    strategy:
        Splitting heuristic to use.  SENTENCE and PARAGRAPH strategies
        automatically fall back to CHARACTER for any segment that exceeds
        *max_size*.
    max_size:
        Maximum number of characters per chunk.  Every returned chunk is
        guaranteed to satisfy ``len(chunk.text) <= max_size``.
    overlap:
        Number of trailing characters from the previous chunk to prepend to
        the next chunk (sliding-window context).  Only used by CHARACTER
        strategy directly; SENTENCE and PARAGRAPH carry the tail of a full
        accumulated buffer.

    Returns
    -------
    list[Chunk]
        Ordered, zero-indexed list of chunks.  Empty input returns ``[]``.
    """
    if not content:
        return []
    if max_size < 1:
        raise ValueError(f"max_size must be >= 1, got {max_size}")
    overlap = max(0, min(overlap, max_size - 1))

    if strategy is ChunkStrategy.CHARACTER:
        chunks = _character_chunks(content, max_size, overlap)
    elif strategy is ChunkStrategy.SENTENCE:
        chunks = _sentence_chunks(content, max_size, overlap)
    elif strategy is ChunkStrategy.PARAGRAPH:
        chunks = _paragraph_chunks(content, max_size, overlap)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Re-index to ensure a clean 0-based sequence (fallbacks may shift indices)
    for i, chunk in enumerate(chunks):
        chunk.index = i

    return chunks


_EXT_MAP: dict[str, ChunkStrategy] = {
    ".py": ChunkStrategy.PARAGRAPH,
    ".go": ChunkStrategy.PARAGRAPH,
    ".ts": ChunkStrategy.PARAGRAPH,
    ".js": ChunkStrategy.PARAGRAPH,
    ".java": ChunkStrategy.PARAGRAPH,
    ".rs": ChunkStrategy.PARAGRAPH,
    ".md": ChunkStrategy.SENTENCE,
    ".txt": ChunkStrategy.SENTENCE,
    ".rst": ChunkStrategy.SENTENCE,
    ".yaml": ChunkStrategy.CHARACTER,
    ".yml": ChunkStrategy.CHARACTER,
    ".json": ChunkStrategy.CHARACTER,
    ".toml": ChunkStrategy.CHARACTER,
    ".tf": ChunkStrategy.CHARACTER,
    ".hcl": ChunkStrategy.CHARACTER,
}


def detect_strategy(file_ext: str) -> ChunkStrategy:
    """Return the recommended :class:`ChunkStrategy` for a file extension.

    *file_ext* should include the leading dot (e.g. ``".py"``).
    Comparison is case-insensitive.  Unknown extensions default to SENTENCE.
    """
    return _EXT_MAP.get(file_ext.lower(), ChunkStrategy.SENTENCE)

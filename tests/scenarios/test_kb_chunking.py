"""Scenario tests for KB chunking reliability.

Covers the public surface of cap.lib.chunker (chunk_content, detect_strategy)
with scenario-level inputs: realistic file sizes, dense single-line content,
language-specific strategies, overlap semantics, and hash uniqueness.

Each test is independent and completes well under 100 ms.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.lib.chunker import Chunk, ChunkStrategy, chunk_content, detect_strategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _assert_bounded(chunks: list[Chunk], max_size: int) -> None:
    for c in chunks:
        assert len(c.text) <= max_size, (
            f"Chunk {c.index} has {len(c.text)} chars > max_size={max_size}"
        )


def _make_python_file(approx_bytes: int) -> str:
    """Produce a plausible Python file of approximately *approx_bytes* chars."""
    block = (
        "def function_{n}(arg1, arg2):\n"
        "    \"\"\"Docstring for function {n}.\"\"\"\n"
        "    result = arg1 + arg2\n"
        "    return result\n\n"
    )
    lines = []
    total = 0
    n = 0
    while total < approx_bytes:
        chunk = block.format(n=n)
        lines.append(chunk)
        total += len(chunk)
        n += 1
    return "".join(lines)


def _make_markdown(approx_bytes: int) -> str:
    """Produce Markdown content of approximately *approx_bytes* chars."""
    sentence = (
        "This section describes the feature in detail. "
        "It covers all edge cases and provides examples. "
        "See the related documentation for more context.\n"
    )
    times = max(1, approx_bytes // len(sentence))
    return sentence * times


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------


class TestLargePythonFileProducesBoundedChunks:
    """10 KB Python content — every produced chunk must be <= 1024 chars."""

    def test_large_python_file_produces_bounded_chunks(self) -> None:
        content = _make_python_file(10_000)
        assert len(content) >= 8_000, "Fixture too small"
        chunks = chunk_content(content, ChunkStrategy.PARAGRAPH, max_size=1024, overlap=100)
        assert len(chunks) > 1
        _assert_bounded(chunks, 1024)


class TestRecursiveFallbackForDenseContent:
    """A single 5000-char line with no split boundaries must fall back to CHARACTER."""

    def test_recursive_fallback_for_dense_content(self) -> None:
        # No sentence/paragraph boundaries — the strategies must fall back.
        content = "X" * 5000
        for strategy in (ChunkStrategy.SENTENCE, ChunkStrategy.PARAGRAPH):
            chunks = chunk_content(content, strategy, max_size=512, overlap=50)
            assert len(chunks) > 1, f"Expected multiple chunks for strategy {strategy}"
            _assert_bounded(chunks, 512)


class TestParagraphStrategyOnCode:
    """Content with blank-line separators splits on paragraph boundaries."""

    def test_paragraph_strategy_on_code(self) -> None:
        # Three distinct code blocks separated by blank lines.
        block_a = "def alpha():\n    pass"
        block_b = "def beta():\n    return 1"
        block_c = "class Gamma:\n    x = 42"
        content = f"{block_a}\n\n{block_b}\n\n{block_c}"

        chunks = chunk_content(content, ChunkStrategy.PARAGRAPH, max_size=1024, overlap=0)
        # All three blocks are small enough to merge into one chunk (total < 1024),
        # but the strategy must recognise paragraph structure and not raise.
        assert len(chunks) >= 1
        _assert_bounded(chunks, 1024)

        # With a tiny max_size that forces each block to its own chunk:
        small_chunks = chunk_content(content, ChunkStrategy.PARAGRAPH, max_size=30, overlap=0)
        assert len(small_chunks) >= 3
        _assert_bounded(small_chunks, 30)


class TestSentenceStrategyOnMarkdown:
    """Markdown content splits on sentence boundaries (. ! ?)."""

    def test_sentence_strategy_on_markdown(self) -> None:
        md = (
            "# Overview\n\n"
            "This is the first sentence. Here is the second sentence! "
            "Is this the third? Yes it is.\n\n"
            "## Details\n\n"
            "Another paragraph begins here. It continues across multiple sentences."
        )
        chunks = chunk_content(md, ChunkStrategy.SENTENCE, max_size=80, overlap=10)
        assert len(chunks) >= 2
        _assert_bounded(chunks, 80)


class TestOverlapPreservesContext:
    """Adjacent chunks share overlap chars when CHARACTER strategy is used."""

    def test_overlap_preserves_context(self) -> None:
        # Use a known content so overlap positions are predictable.
        content = "abcdefghijklmnopqrstuvwxyz"  # 26 chars
        max_size = 10
        overlap = 4
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=max_size, overlap=overlap)
        assert len(chunks) >= 2
        # The tail of chunk[0] must appear at the start of chunk[1].
        tail_of_first = chunks[0].text[-overlap:]
        head_of_second = chunks[1].text[:overlap]
        assert tail_of_first == head_of_second, (
            f"Expected overlap '{tail_of_first}' at start of chunk[1], got '{head_of_second}'"
        )


class TestContentHashUniquePerChunk:
    """Each chunk produced from varied content receives a unique SHA-256 hash."""

    def test_content_hash_unique_per_chunk(self) -> None:
        # Build content where each paragraph is distinct so hashes must differ.
        paragraphs = [f"Unique paragraph number {i} with distinct text." for i in range(20)]
        content = "\n\n".join(paragraphs)
        chunks = chunk_content(content, ChunkStrategy.PARAGRAPH, max_size=200, overlap=0)
        hashes = [c.content_hash for c in chunks]
        assert len(hashes) == len(set(hashes)), "Duplicate content hashes detected"

    def test_content_hash_is_sha256_prefix(self) -> None:
        content = "Hello, world!"
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=1024)
        assert len(chunks) == 1
        expected = _sha256(content)
        assert chunks[0].content_hash == expected


class TestDetectStrategyForCommonExtensions:
    """detect_strategy maps well-known extensions to the expected strategy."""

    @pytest.mark.parametrize("ext", [".py", ".go", ".ts", ".js", ".java", ".rs"])
    def test_code_extension_returns_paragraph(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.PARAGRAPH

    @pytest.mark.parametrize("ext", [".md", ".txt", ".rst"])
    def test_markdown_extension_returns_sentence(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.SENTENCE

    @pytest.mark.parametrize("ext", [".yaml", ".yml", ".json", ".toml", ".tf", ".hcl"])
    def test_config_extension_returns_character(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.CHARACTER

    def test_detect_py_returns_paragraph(self) -> None:
        assert detect_strategy(".py") is ChunkStrategy.PARAGRAPH

    def test_detect_md_returns_sentence(self) -> None:
        assert detect_strategy(".md") is ChunkStrategy.SENTENCE

    def test_detect_yaml_returns_character(self) -> None:
        assert detect_strategy(".yaml") is ChunkStrategy.CHARACTER

    def test_case_insensitive(self) -> None:
        assert detect_strategy(".PY") is ChunkStrategy.PARAGRAPH
        assert detect_strategy(".MD") is ChunkStrategy.SENTENCE
        assert detect_strategy(".YAML") is ChunkStrategy.CHARACTER

    def test_unknown_extension_defaults_to_sentence(self) -> None:
        assert detect_strategy(".xyz") is ChunkStrategy.SENTENCE


class TestEmptyContentReturnsEmptyList:
    """Edge case: empty string input produces an empty chunk list."""

    def test_empty_content_returns_empty_list(self) -> None:
        for strategy in ChunkStrategy:
            result = chunk_content("", strategy, max_size=1024)
            assert result == [], f"Expected [] for strategy {strategy}, got {result}"


class TestContentUnderMaxSizeReturnsSingleChunk:
    """Small content that fits within max_size produces exactly one chunk."""

    def test_content_under_max_size_returns_single_chunk(self) -> None:
        content = "Small file content that fits in a single chunk."
        for strategy in ChunkStrategy:
            chunks = chunk_content(content, strategy, max_size=1024, overlap=0)
            assert len(chunks) == 1, (
                f"Expected 1 chunk for strategy {strategy}, got {len(chunks)}"
            )
            assert chunks[0].text == content

    def test_exactly_max_size_is_single_chunk(self) -> None:
        content = "a" * 512
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=512, overlap=0)
        assert len(chunks) == 1
        assert chunks[0].text == content

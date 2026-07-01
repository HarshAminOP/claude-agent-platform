"""Unit tests for cap.lib.chunker."""

from __future__ import annotations

import hashlib

import pytest

from cap.lib.chunker import (
    Chunk,
    ChunkStrategy,
    chunk_content,
    detect_strategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def assert_no_chunk_exceeds(chunks: list[Chunk], max_size: int) -> None:
    for c in chunks:
        assert len(c.text) <= max_size, (
            f"Chunk {c.index} has {len(c.text)} chars, exceeds max_size={max_size}: {c.text!r}"
        )


def assert_indices_contiguous(chunks: list[Chunk]) -> None:
    for i, c in enumerate(chunks):
        assert c.index == i, f"Expected index {i}, got {c.index}"


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------


class TestChunkDataclass:
    def test_fields_stored(self) -> None:
        c = Chunk(text="hello", index=0, start_pos=0, end_pos=5, content_hash=_hash("hello"))
        assert c.text == "hello"
        assert c.index == 0
        assert c.start_pos == 0
        assert c.end_pos == 5

    def test_content_hash_format(self) -> None:
        c = Chunk(text="abc", index=0, start_pos=0, end_pos=3, content_hash=_hash("abc"))
        assert len(c.content_hash) == 32
        assert all(ch in "0123456789abcdef" for ch in c.content_hash)

    def test_metadata_defaults_to_empty_dict(self) -> None:
        c = Chunk(text="x", index=0, start_pos=0, end_pos=1, content_hash=_hash("x"))
        assert c.metadata == {}

    def test_metadata_isolated_per_instance(self) -> None:
        c1 = Chunk(text="a", index=0, start_pos=0, end_pos=1, content_hash=_hash("a"))
        c2 = Chunk(text="b", index=1, start_pos=1, end_pos=2, content_hash=_hash("b"))
        c1.metadata["key"] = "value"
        assert "key" not in c2.metadata


# ---------------------------------------------------------------------------
# CHARACTER strategy
# ---------------------------------------------------------------------------


class TestCharacterStrategy:
    def test_empty_content(self) -> None:
        assert chunk_content("", ChunkStrategy.CHARACTER) == []

    def test_single_chunk_fits(self) -> None:
        chunks = chunk_content("hello", ChunkStrategy.CHARACTER, max_size=10, overlap=0)
        assert len(chunks) == 1
        assert chunks[0].text == "hello"

    def test_exact_max_size(self) -> None:
        content = "a" * 100
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=100, overlap=0)
        assert len(chunks) == 1
        assert chunks[0].text == content

    def test_splits_into_multiple(self) -> None:
        content = "a" * 50
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=20, overlap=0)
        assert len(chunks) == 3  # 20 + 20 + 10
        assert_no_chunk_exceeds(chunks, 20)

    def test_overlap_creates_sliding_window(self) -> None:
        content = "abcdefghij"  # 10 chars
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=6, overlap=2)
        # step = 6 - 2 = 4; windows: [0:6], [4:10]
        assert chunks[0].text == "abcdef"
        assert chunks[1].text == "efghij"

    def test_no_chunk_exceeds_max_size(self) -> None:
        import random
        random.seed(42)
        content = "".join(random.choices("abcdefghijklmnopqrstuvwxyz \n", k=5000))
        for max_size in (50, 200, 1024):
            chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=max_size, overlap=50)
            assert_no_chunk_exceeds(chunks, max_size)

    def test_indices_are_contiguous(self) -> None:
        content = "x" * 300
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=100, overlap=10)
        assert_indices_contiguous(chunks)

    def test_start_end_positions(self) -> None:
        content = "abcde"
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=3, overlap=0)
        assert chunks[0].start_pos == 0
        assert chunks[0].end_pos == 3
        assert chunks[1].start_pos == 3
        assert chunks[1].end_pos == 5

    def test_content_hash_matches(self) -> None:
        content = "hello world"
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=1024)
        assert chunks[0].content_hash == _hash(content)

    def test_overlap_clamped_to_max_size_minus_one(self) -> None:
        # overlap >= max_size should not infinite-loop
        content = "abc" * 100
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=10, overlap=20)
        assert len(chunks) > 0
        assert_no_chunk_exceeds(chunks, 10)


# ---------------------------------------------------------------------------
# SENTENCE strategy
# ---------------------------------------------------------------------------


class TestSentenceStrategy:
    def test_empty(self) -> None:
        assert chunk_content("", ChunkStrategy.SENTENCE) == []

    def test_short_content_single_chunk(self) -> None:
        text = "Hello world."
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=1024)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_splits_on_period_space(self) -> None:
        text = "Sentence one. Sentence two. Sentence three."
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=20, overlap=0)
        assert_no_chunk_exceeds(chunks, 20)
        assert len(chunks) >= 2

    def test_splits_on_exclamation(self) -> None:
        text = "Hey! Stop! Look!"
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=10, overlap=0)
        assert_no_chunk_exceeds(chunks, 10)

    def test_splits_on_question(self) -> None:
        text = "Who? What? When?"
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=10, overlap=0)
        assert_no_chunk_exceeds(chunks, 10)

    def test_splits_on_blank_line(self) -> None:
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=20, overlap=0)
        assert_no_chunk_exceeds(chunks, 20)

    def test_character_fallback_for_long_sentence(self) -> None:
        # Single very long sentence with no boundary inside
        long_sentence = "A" * 500
        chunks = chunk_content(long_sentence, ChunkStrategy.SENTENCE, max_size=100, overlap=10)
        assert_no_chunk_exceeds(chunks, 100)
        assert len(chunks) > 1

    def test_no_chunk_exceeds_max_size_fuzz(self) -> None:
        text = (
            "This is sentence one. This is sentence two! Is this three? "
            "And this is a very long sentence that goes on and on " * 30
            + " done."
        )
        for max_size in (50, 200):
            chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=max_size, overlap=20)
            assert_no_chunk_exceeds(chunks, max_size)

    def test_indices_contiguous(self) -> None:
        text = "One. Two. Three. Four. Five."
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=10, overlap=0)
        assert_indices_contiguous(chunks)

    def test_reconstructs_content(self) -> None:
        # All chunk texts together should cover the original content
        text = "Hello world. Goodbye world!"
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=1024, overlap=0)
        combined = "".join(c.text for c in chunks)
        assert combined == text


# ---------------------------------------------------------------------------
# PARAGRAPH strategy
# ---------------------------------------------------------------------------


class TestParagraphStrategy:
    def test_empty(self) -> None:
        assert chunk_content("", ChunkStrategy.PARAGRAPH) == []

    def test_single_paragraph(self) -> None:
        text = "def foo():\n    pass"
        chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=1024)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_splits_on_blank_lines(self) -> None:
        text = "block one\n\nblock two\n\nblock three"
        chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=15, overlap=0)
        assert_no_chunk_exceeds(chunks, 15)
        assert len(chunks) >= 2

    def test_merges_small_paragraphs(self) -> None:
        text = "a\n\nb\n\nc"
        chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=1024, overlap=0)
        assert len(chunks) == 1

    def test_character_fallback_for_long_paragraph(self) -> None:
        big_para = "x" * 2000
        text = f"intro\n\n{big_para}\n\noutro"
        chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=100, overlap=10)
        assert_no_chunk_exceeds(chunks, 100)

    def test_no_chunk_exceeds_max_size_fuzz(self) -> None:
        paras = [f"Paragraph {i}: " + "word " * 50 for i in range(20)]
        text = "\n\n".join(paras)
        for max_size in (100, 500):
            chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=max_size, overlap=30)
            assert_no_chunk_exceeds(chunks, max_size)

    def test_indices_contiguous(self) -> None:
        text = "p1\n\np2\n\np3\n\np4"
        chunks = chunk_content(text, ChunkStrategy.PARAGRAPH, max_size=5, overlap=0)
        assert_indices_contiguous(chunks)


# ---------------------------------------------------------------------------
# detect_strategy
# ---------------------------------------------------------------------------


class TestDetectStrategy:
    @pytest.mark.parametrize("ext", [".py", ".go", ".ts", ".js", ".java", ".rs"])
    def test_code_extensions_return_paragraph(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.PARAGRAPH

    @pytest.mark.parametrize("ext", [".md", ".txt", ".rst"])
    def test_text_extensions_return_sentence(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.SENTENCE

    @pytest.mark.parametrize("ext", [".yaml", ".yml", ".json", ".toml", ".tf", ".hcl"])
    def test_config_extensions_return_character(self, ext: str) -> None:
        assert detect_strategy(ext) is ChunkStrategy.CHARACTER

    def test_unknown_extension_returns_sentence(self) -> None:
        assert detect_strategy(".xyz") is ChunkStrategy.SENTENCE

    def test_case_insensitive(self) -> None:
        assert detect_strategy(".PY") is ChunkStrategy.PARAGRAPH
        assert detect_strategy(".MD") is ChunkStrategy.SENTENCE
        assert detect_strategy(".YAML") is ChunkStrategy.CHARACTER

    def test_no_leading_dot_unknown(self) -> None:
        # Without dot it just won't match — defaults to SENTENCE
        assert detect_strategy("py") is ChunkStrategy.SENTENCE


# ---------------------------------------------------------------------------
# Edge cases / validation
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_max_size_one(self) -> None:
        content = "abc"
        chunks = chunk_content(content, ChunkStrategy.CHARACTER, max_size=1, overlap=0)
        assert len(chunks) == 3
        assert all(len(c.text) == 1 for c in chunks)

    def test_invalid_max_size_raises(self) -> None:
        with pytest.raises(ValueError, match="max_size"):
            chunk_content("hello", max_size=0)

    def test_negative_overlap_treated_as_zero(self) -> None:
        # Should not raise
        chunks = chunk_content("abc" * 100, ChunkStrategy.CHARACTER, max_size=50, overlap=-10)
        assert_no_chunk_exceeds(chunks, 50)

    def test_whitespace_only_content(self) -> None:
        # Should not crash; may return empty or whitespace chunk
        chunks = chunk_content("   ", ChunkStrategy.SENTENCE, max_size=1024)
        # All content accounted for, no exception
        assert isinstance(chunks, list)

    def test_unicode_content(self) -> None:
        text = "日本語テスト。これはチャンクです。終わり。"
        chunks = chunk_content(text, ChunkStrategy.SENTENCE, max_size=20, overlap=0)
        assert_no_chunk_exceeds(chunks, 20)

    def test_all_strategies_on_same_content(self) -> None:
        content = "Line one.\n\nLine two.\n\nLine three."
        for strategy in ChunkStrategy:
            chunks = chunk_content(content, strategy, max_size=50, overlap=5)
            assert_no_chunk_exceeds(chunks, 50)
            assert_indices_contiguous(chunks)

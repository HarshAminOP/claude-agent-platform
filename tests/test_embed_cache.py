"""Tests for cap.lib.embed_cache.PersistentEmbedCache."""

import sqlite3
import struct
from collections.abc import Generator
from datetime import datetime, timezone, timedelta

import pytest

from cap.lib.embed_cache import PersistentEmbedCache, VECTOR_DIM, _PACK_FMT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db() -> Generator[sqlite3.Connection, None, None]:
    """In-memory SQLite connection for each test."""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture()
def cache(db: sqlite3.Connection) -> PersistentEmbedCache:
    return PersistentEmbedCache(db, max_size=5, ttl_days=7)


def _vec(seed: float = 0.1) -> list[float]:
    """Return a valid VECTOR_DIM-length float list."""
    return [seed] * VECTOR_DIM


# ---------------------------------------------------------------------------
# Construction / DDL
# ---------------------------------------------------------------------------

class TestInit:
    def test_table_created(self, db: sqlite3.Connection) -> None:
        PersistentEmbedCache(db)
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "embedding_cache" in tables

    def test_index_created(self, db: sqlite3.Connection) -> None:
        PersistentEmbedCache(db)
        indexes = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_embedding_cache_accessed" in indexes

    def test_invalid_max_size(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="max_size"):
            PersistentEmbedCache(db, max_size=0)

    def test_invalid_ttl(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="ttl_days"):
            PersistentEmbedCache(db, ttl_days=0)

    def test_idempotent_init(self, db: sqlite3.Connection) -> None:
        """Calling __init__ twice on the same DB must not raise."""
        PersistentEmbedCache(db)
        PersistentEmbedCache(db)


# ---------------------------------------------------------------------------
# get / put round-trip
# ---------------------------------------------------------------------------

class TestGetPut:
    def test_miss_returns_none(self, cache: PersistentEmbedCache) -> None:
        assert cache.get("nonexistent") is None

    def test_put_then_get(self, cache: PersistentEmbedCache) -> None:
        vec = _vec(0.42)
        cache.put("h1", vec)
        result = cache.get("h1")
        assert result is not None
        assert len(result) == VECTOR_DIM
        # float32 round-trip precision
        assert all(abs(a - b) < 1e-6 for a, b in zip(result, vec))

    def test_put_replaces_existing(self, cache: PersistentEmbedCache) -> None:
        cache.put("h1", _vec(0.1))
        new_vec = _vec(0.9)
        cache.put("h1", new_vec)
        result = cache.get("h1")
        assert abs(result[0] - 0.9) < 1e-6

    def test_wrong_dim_raises(self, cache: PersistentEmbedCache) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            cache.put("h1", [0.1, 0.2])

    def test_get_updates_accessed_at(
        self, cache: PersistentEmbedCache, db: sqlite3.Connection
    ) -> None:
        cache.put("h1", _vec())
        before = db.execute(
            "SELECT accessed_at FROM embedding_cache WHERE content_hash='h1'"
        ).fetchone()[0]

        import time
        time.sleep(1.1)  # ensure the wall-clock second advances

        cache.get("h1")
        after = db.execute(
            "SELECT accessed_at FROM embedding_cache WHERE content_hash='h1'"
        ).fetchone()[0]

        assert after > before

    def test_serialisation_format(
        self, cache: PersistentEmbedCache, db: sqlite3.Connection
    ) -> None:
        vec = _vec(0.5)
        cache.put("h1", vec)
        blob = db.execute(
            "SELECT vector FROM embedding_cache WHERE content_hash='h1'"
        ).fetchone()[0]
        expected = struct.pack(_PACK_FMT, *vec)
        assert blob == expected


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

class TestLRUEviction:
    @staticmethod
    def _force_accessed_at(
        db: sqlite3.Connection, content_hash: str, ts: str
    ) -> None:
        db.execute(
            "UPDATE embedding_cache SET accessed_at = ? WHERE content_hash = ?",
            (ts, content_hash),
        )
        db.commit()

    def test_evicts_when_full(self, db: sqlite3.Connection) -> None:
        cache = PersistentEmbedCache(db, max_size=3, ttl_days=7)
        for i in range(3):
            cache.put(f"h{i}", _vec(float(i)))

        # Pin explicit timestamps so the LRU order is deterministic.
        # h2 is oldest → should be evicted; h0 and h1 are newer.
        self._force_accessed_at(db, "h2", "2020-01-01T00:00:00Z")
        self._force_accessed_at(db, "h0", "2020-01-02T00:00:00Z")
        self._force_accessed_at(db, "h1", "2020-01-03T00:00:00Z")

        # Insert a 4th entry — h2 should be evicted
        cache.put("h3", _vec(3.0))

        assert cache.get("h2") is None
        assert cache.get("h0") is not None
        assert cache.get("h1") is not None
        assert cache.get("h3") is not None

    def test_replace_does_not_grow(self, db: sqlite3.Connection) -> None:
        """Replacing an existing key must NOT trigger LRU eviction."""
        cache = PersistentEmbedCache(db, max_size=2, ttl_days=7)
        cache.put("h0", _vec(0.0))
        cache.put("h1", _vec(1.0))

        # Replace h0 — size must stay at 2, not evict h1
        cache.put("h0", _vec(99.0))

        assert cache.get("h1") is not None
        s = cache.stats()
        assert s["size"] == 2

    def test_size_stays_at_max(self, db: sqlite3.Connection) -> None:
        cache = PersistentEmbedCache(db, max_size=3, ttl_days=7)
        for i in range(10):
            cache.put(f"h{i}", _vec(float(i)))
        assert cache.stats()["size"] <= 3


# ---------------------------------------------------------------------------
# evict_expired
# ---------------------------------------------------------------------------

class TestEvictExpired:
    def _set_accessed_at(
        self, db: sqlite3.Connection, content_hash: str, dt: datetime
    ) -> None:
        ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "UPDATE embedding_cache SET accessed_at = ? WHERE content_hash = ?",
            (ts, content_hash),
        )
        db.commit()

    def test_evicts_old_entries(
        self, cache: PersistentEmbedCache, db: sqlite3.Connection
    ) -> None:
        cache.put("old", _vec(0.1))
        cache.put("new", _vec(0.2))

        old_ts = datetime.now(tz=timezone.utc) - timedelta(days=8)
        self._set_accessed_at(db, "old", old_ts)

        deleted = cache.evict_expired()
        assert deleted == 1
        assert cache.get("old") is None
        assert cache.get("new") is not None

    def test_returns_zero_when_nothing_expired(
        self, cache: PersistentEmbedCache
    ) -> None:
        cache.put("h1", _vec())
        assert cache.evict_expired() == 0

    def test_returns_correct_count(
        self, cache: PersistentEmbedCache, db: sqlite3.Connection
    ) -> None:
        for i in range(4):
            cache.put(f"h{i}", _vec(float(i)))

        old_ts = datetime.now(tz=timezone.utc) - timedelta(days=10)
        for i in range(3):
            self._set_accessed_at(db, f"h{i}", old_ts)

        assert cache.evict_expired() == 3


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_empty_cache(self, cache: PersistentEmbedCache) -> None:
        s = cache.stats()
        assert s["size"] == 0
        assert s["max_size"] == 5
        assert s["ttl_days"] == 7
        assert s["oldest_accessed"] is None

    def test_populated_cache(self, cache: PersistentEmbedCache) -> None:
        cache.put("h1", _vec())
        cache.put("h2", _vec())
        s = cache.stats()
        assert s["size"] == 2
        assert s["oldest_accessed"] is not None

    def test_oldest_accessed_is_string(self, cache: PersistentEmbedCache) -> None:
        cache.put("h1", _vec())
        s = cache.stats()
        assert isinstance(s["oldest_accessed"], str)


# ---------------------------------------------------------------------------
# purge_for_entry
# ---------------------------------------------------------------------------

class TestPurgeForEntry:
    def test_removes_entry(self, cache: PersistentEmbedCache) -> None:
        cache.put("h1", _vec())
        cache.purge_for_entry("h1")
        assert cache.get("h1") is None

    def test_noop_on_missing_key(self, cache: PersistentEmbedCache) -> None:
        """Should not raise even if the key does not exist."""
        cache.purge_for_entry("does-not-exist")

    def test_only_removes_target(self, cache: PersistentEmbedCache) -> None:
        cache.put("h1", _vec(0.1))
        cache.put("h2", _vec(0.2))
        cache.purge_for_entry("h1")
        assert cache.get("h2") is not None
        assert cache.stats()["size"] == 1

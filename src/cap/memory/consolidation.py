"""
CAP Cross-Session Memory Consolidation.

Groups similar memory entries via FTS5 BM25 scoring and union-find clustering,
then merges clusters of 3+ entries into consolidated summaries.

Algorithm:
  1. find_similar_clusters(): FTS5 BM25 score > 0.7, group via union-find, min cluster 3
  2. summarize_cluster(): TF-IDF density scoring, preserve decision rationale sentences
     (because/decided/chose/rejected markers), 500 token budget
  3. Mark originals as consolidated_into = summary_id
"""

import re
import time
import uuid
import math
import sqlite3
from typing import List, Dict, Tuple
from collections import defaultdict


def consolidate(db: sqlite3.Connection) -> dict:
    """
    Run cross-session memory consolidation.

    Finds clusters of similar entries, merges groups of 3+ into single
    consolidated entries, marks originals.

    Args:
        db: SQLite connection with CAP schema.

    Returns:
        Dict with stats: {merge_count, clusters_found, entries_processed}.
    """
    now = time.time()
    stats = {
        "merge_count": 0,
        "clusters_found": 0,
        "entries_processed": 0,
    }

    # Fetch all active non-consolidated entries
    entries = db.execute(
        """SELECT id, content, category, importance, access_count, workspace
           FROM memory_active
           WHERE consolidated_into IS NULL
           ORDER BY category, created_at"""
    ).fetchall()

    if not entries or len(entries) < 3:
        return stats

    # Convert to list of dicts for processing
    entry_list = []
    for row in entries:
        if isinstance(row, sqlite3.Row):
            entry_list.append({
                "id": row["id"],
                "content": row["content"],
                "category": row["category"],
                "importance": row["importance"],
                "access_count": row["access_count"],
                "workspace": row["workspace"],
            })
        else:
            entry_list.append({
                "id": row[0],
                "content": row[1],
                "category": row[2],
                "importance": row[3],
                "access_count": row[4],
                "workspace": row[5],
            })

    stats["entries_processed"] = len(entry_list)

    # Find clusters of similar entries
    clusters = find_similar_clusters(entry_list, db, threshold=0.7)
    stats["clusters_found"] = len(clusters)

    # Merge clusters with 3+ entries
    for cluster in clusters:
        if len(cluster) < 3:
            continue

        merged_content = summarize_cluster([e["content"] for e in cluster])
        merged_importance = max(e["importance"] for e in cluster)
        merged_access_count = sum(e["access_count"] for e in cluster)
        workspace = cluster[0]["workspace"]
        category = cluster[0]["category"]

        merged_id = str(uuid.uuid4())
        token_count = len(merged_content) // 4

        try:
            db.execute(
                """INSERT INTO memory_active
                   (id, workspace, category, content, token_count, created_at,
                    last_accessed, access_count, importance, composite_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    merged_id,
                    workspace,
                    category,
                    merged_content,
                    token_count,
                    now,
                    now,
                    merged_access_count,
                    merged_importance,
                    merged_importance,
                ),
            )

            # Mark originals as consolidated
            for entry in cluster:
                db.execute(
                    "UPDATE memory_active SET consolidated_into = ? WHERE id = ?",
                    (merged_id, entry["id"]),
                )

            stats["merge_count"] += 1
        except sqlite3.IntegrityError:
            continue

    db.commit()
    return stats


def find_similar_clusters(
    entries: List[Dict], db: sqlite3.Connection, threshold: float = 0.7
) -> List[List[Dict]]:
    """
    Find clusters of similar entries using FTS5 BM25 scoring + union-find.

    For each entry, query its key terms against FTS5. If BM25 similarity
    exceeds threshold, union the two entries. Return clusters with 3+ members.

    Args:
        entries: List of entry dicts with 'id', 'content', 'category'.
        db: SQLite connection for FTS5 queries.
        threshold: Minimum BM25 similarity score for grouping.

    Returns:
        List of clusters (each cluster is a list of entry dicts).
    """
    if len(entries) < 3:
        return []

    # Build index: id -> entry, id -> position
    id_to_idx = {}
    for idx, entry in enumerate(entries):
        id_to_idx[entry["id"]] = idx

    # Union-Find data structure
    parent = list(range(len(entries)))
    rank = [0] * len(entries)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        if rank[rx] < rank[ry]:
            rx, ry = ry, rx
        parent[ry] = rx
        if rank[rx] == rank[ry]:
            rank[rx] += 1

    # For each entry, extract key terms and query FTS5
    for i, entry in enumerate(entries):
        key_terms = _extract_key_terms(entry["content"])
        if not key_terms:
            continue

        # Query FTS5 for similar entries
        safe_query = " OR ".join(key_terms[:5])  # Limit to 5 terms
        try:
            rows = db.execute(
                """SELECT a.id, rank
                   FROM memory_active a
                   JOIN memory_fts ON memory_fts.rowid = a.rowid
                   WHERE memory_fts MATCH ?
                   AND a.id != ?
                   AND a.consolidated_into IS NULL
                   AND a.category = ?
                   LIMIT 20""",
                (safe_query, entry["id"], entry["category"]),
            ).fetchall()
        except sqlite3.OperationalError:
            continue

        if not rows:
            continue

        # Normalize BM25 ranks across results
        max_rank = max(abs(r[1] if not isinstance(r, sqlite3.Row) else r["rank"]) for r in rows) or 1.0

        for row in rows:
            other_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            bm25 = row["rank"] if isinstance(row, sqlite3.Row) else row[1]

            # Normalize to 0-1 (higher = more similar)
            similarity = abs(bm25) / max_rank if max_rank > 0 else 0

            if similarity >= threshold and other_id in id_to_idx:
                j = id_to_idx[other_id]
                union(i, j)

    # Collect clusters
    cluster_map = defaultdict(list)
    for idx, entry in enumerate(entries):
        root = find(idx)
        cluster_map[root].append(entry)

    # Return only clusters with 3+ members
    return [cluster for cluster in cluster_map.values() if len(cluster) >= 3]


def summarize_cluster(contents: List[str], max_tokens: int = 500) -> str:
    """
    Deterministic summarization without LLM call.

    Algorithm:
      - Split all content into sentences
      - Score sentences by TF-IDF density (unique terms / total terms)
      - Preserve decision rationale sentences (because/decided/chose/rejected)
      - Deduplicate near-identical sentences
      - Truncate to 500 token budget

    Args:
        contents: List of content strings to merge.
        max_tokens: Maximum token budget for output.

    Returns:
        Consolidated summary string.
    """
    max_chars = max_tokens * 4  # Approximate: 4 chars per token

    # Split into sentences
    all_sentences = []
    for content in contents:
        sentences = _split_sentences(content)
        all_sentences.extend(sentences)

    if not all_sentences:
        return " ".join(contents)[:max_chars]

    # Compute term frequencies across all sentences (for IDF)
    doc_freq = defaultdict(int)
    total_docs = len(all_sentences)
    sentence_terms = []

    for sentence in all_sentences:
        terms = set(_tokenize(sentence))
        sentence_terms.append(terms)
        for term in terms:
            doc_freq[term] += 1

    # Score each sentence by TF-IDF density
    scored_sentences = []
    for idx, sentence in enumerate(all_sentences):
        terms = sentence_terms[idx]
        if not terms:
            continue

        # TF-IDF density: sum of IDF weights / sentence length
        idf_sum = 0.0
        for term in terms:
            df = doc_freq.get(term, 1)
            idf = math.log((total_docs + 1) / (df + 1))
            idf_sum += idf

        density = idf_sum / max(len(terms), 1)

        # Boost decision rationale sentences
        is_rationale = _is_rationale_sentence(sentence)
        if is_rationale:
            density *= 2.0

        scored_sentences.append((density, idx, sentence, is_rationale))

    # Sort: rationale sentences first, then by density
    scored_sentences.sort(key=lambda x: (not x[3], -x[0]))

    # Deduplicate near-identical sentences
    selected = []
    selected_normalized = set()
    chars_used = 0

    for _, _, sentence, _ in scored_sentences:
        normalized = _normalize_for_dedup(sentence)
        if normalized in selected_normalized:
            continue
        if chars_used + len(sentence) > max_chars:
            remaining = max_chars - chars_used
            if remaining > 20:
                selected.append(sentence[:remaining])
            break
        selected.append(sentence)
        selected_normalized.add(normalized)
        chars_used += len(sentence) + 1  # +1 for space/newline

    return " ".join(selected) if selected else contents[0][:max_chars]


def _extract_key_terms(text: str, max_terms: int = 8) -> List[str]:
    """
    Extract key terms from text for FTS5 querying.
    Filters out stop words and short terms.
    """
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "and", "but", "or",
        "not", "no", "nor", "so", "yet", "both", "either", "neither", "each",
        "every", "all", "any", "few", "more", "most", "other", "some", "such",
        "than", "too", "very", "just", "also", "that", "this", "these", "those",
        "it", "its", "they", "them", "their", "we", "us", "our", "you", "your",
        "he", "him", "his", "she", "her", "i", "me", "my", "if", "then", "else",
        "when", "where", "which", "what", "who", "how", "why",
    }

    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    # Filter stop words and deduplicate preserving order
    seen = set()
    terms = []
    for w in words:
        if w not in stop_words and w not in seen:
            seen.add(w)
            terms.append(w)
            if len(terms) >= max_terms:
                break
    return terms


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences."""
    # Split on sentence-ending punctuation or newlines
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 10]


def _tokenize(text: str) -> List[str]:
    """Simple word tokenization."""
    return re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())


def _is_rationale_sentence(sentence: str) -> bool:
    """Check if sentence contains decision rationale markers."""
    markers = ["because", "decided", "chose", "rejected", "reason", "rationale", "chose to", "opted"]
    lower = sentence.lower()
    return any(marker in lower for marker in markers)


def _normalize_for_dedup(sentence: str) -> str:
    """Normalize sentence for deduplication (lowercase, strip punctuation)."""
    return re.sub(r"[^a-z0-9 ]", "", sentence.lower()).strip()

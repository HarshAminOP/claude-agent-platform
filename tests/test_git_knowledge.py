"""Tests for git knowledge ingestion module."""
import pytest
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.git_knowledge import (
    BlameEntry, PRDiscussion, OwnershipMap,
    extract_blame, compute_ownership, fetch_pr_discussions, ingest_git_knowledge,
)


SAMPLE_BLAME_OUTPUT = """\
abc123def456789012345678901234567890abcd 1 1 3
author Alice Smith
author-mail <alice@example.com>
author-time 1700000000
author-tz +0000
committer Alice Smith
committer-mail <alice@example.com>
committer-time 1700000000
committer-tz +0000
summary Initial commit
filename main.py
\tdef hello():
abc123def456789012345678901234567890abcd 2 2
\t    return "world"
bbb456789012345678901234567890123456abcd 3 3 1
author Bob Jones
author-mail <bob@example.com>
author-time 1700100000
author-tz +0000
committer Bob Jones
committer-mail <bob@example.com>
committer-time 1700100000
committer-tz +0000
summary Add docstring
filename main.py
\t    # added by bob
"""


SAMPLE_PR_JSON = json.dumps([
    {
        "number": 42,
        "title": "Fix auth timeout",
        "body": "This PR fixes the auth timeout issue",
        "author": {"login": "alice"},
        "state": "MERGED",
        "comments": [
            {"author": {"login": "bob"}, "body": "LGTM"},
        ],
        "reviewComments": [
            {"author": {"login": "carol"}, "body": "Consider retry logic", "path": "auth.py"},
        ],
        "files": [{"path": "auth.py"}, {"path": "tests/test_auth.py"}],
        "mergedAt": "2024-01-15T10:00:00Z",
        "labels": [{"name": "bug"}, {"name": "auth"}],
    },
])


@patch("cap.lib.git_knowledge.subprocess.run")
def test_extract_blame(mock_run, tmp_path):
    (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n    # added by bob\n")
    mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_BLAME_OUTPUT, stderr="")

    entries = extract_blame("main.py", str(tmp_path))
    assert len(entries) == 3
    assert entries[0].author == "Alice Smith"
    assert entries[0].email == "alice@example.com"
    assert entries[2].author == "Bob Jones"
    assert entries[0].commit_sha == "abc123def456789012345678901234567890abcd"


@patch("cap.lib.git_knowledge.subprocess.run")
def test_extract_blame_missing_file(mock_run, tmp_path):
    entries = extract_blame("nonexistent.py", str(tmp_path))
    assert entries == []
    mock_run.assert_not_called()


@patch("cap.lib.git_knowledge.subprocess.run")
def test_compute_ownership(mock_run, tmp_path):
    (tmp_path / "main.py").write_text("line1\nline2\nline3\n")
    # First call: blame, second call: log
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout=SAMPLE_BLAME_OUTPUT, stderr=""),
        MagicMock(returncode=0, stdout="abc123 Initial commit\nbbb456 Add docstring\n"),
    ]

    ownership = compute_ownership("main.py", str(tmp_path))
    assert ownership is not None
    assert ownership.primary_owner == "Alice Smith"
    assert len(ownership.contributors) == 2
    assert ownership.contributors[0]["author"] == "Alice Smith"
    assert ownership.contributors[0]["lines"] == 2
    assert ownership.change_frequency == 2


@patch("cap.lib.git_knowledge.subprocess.run")
def test_fetch_pr_discussions(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_PR_JSON, stderr="")

    prs = fetch_pr_discussions(str(tmp_path), limit=10)
    assert len(prs) == 1
    assert prs[0].pr_number == 42
    assert prs[0].title == "Fix auth timeout"
    assert prs[0].author == "alice"
    assert len(prs[0].comments) == 1
    assert len(prs[0].review_comments) == 1
    assert prs[0].files_changed == ["auth.py", "tests/test_auth.py"]
    assert prs[0].labels == ["bug", "auth"]


@patch("cap.lib.git_knowledge.subprocess.run")
def test_fetch_pr_discussions_gh_not_found(mock_run):
    mock_run.side_effect = FileNotFoundError()
    prs = fetch_pr_discussions("/some/repo")
    assert prs == []


@patch("cap.lib.git_knowledge.subprocess.run")
def test_fetch_pr_discussions_invalid_json(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
    prs = fetch_pr_discussions(str(tmp_path))
    assert prs == []


@patch("cap.lib.git_knowledge.fetch_pr_discussions")
@patch("cap.lib.git_knowledge.compute_ownership")
def test_ingest_git_knowledge(mock_ownership, mock_prs, tmp_path):
    mock_ownership.return_value = OwnershipMap(
        file_path="src/main.py",
        primary_owner="Alice",
        contributors=[{"author": "Alice", "lines": 100, "pct": 80.0}],
        change_frequency=15,
    )
    mock_prs.return_value = [
        PRDiscussion(
            pr_number=1, title="Feature", body="Added feature X",
            author="bob", state="MERGED", comments=[], review_comments=[],
            files_changed=["src/main.py"],
        ),
    ]

    # Create a minimal knowledge DB
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE knowledge_entries (
        id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, workspace TEXT,
        source_path TEXT, source_type TEXT, content_type TEXT,
        title TEXT, content TEXT, content_hash TEXT, metadata TEXT
    )""")

    stats = ingest_git_knowledge(
        str(tmp_path), db, workspace="test-ws",
        key_files=["src/main.py"],
    )
    assert stats["ownership_entries"] == 1
    assert stats["pr_entries"] == 1
    assert stats["errors"] == 0

    rows = db.execute("SELECT title, source_type FROM knowledge_entries").fetchall()
    assert len(rows) == 2
    titles = {r[0] for r in rows}
    assert any("Ownership" in t for t in titles)
    assert any("PR #1" in t for t in titles)

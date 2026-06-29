"""Git blame + PR discussion knowledge ingestion.

Extracts ownership, change patterns, and PR discussion context from git
history and GitHub PRs, then indexes into the knowledge base for retrieval.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BlameEntry:
    file_path: str
    author: str
    email: str
    date: str
    line_start: int
    line_end: int
    commit_sha: str
    commit_message: str = ""


@dataclass
class PRDiscussion:
    pr_number: int
    title: str
    body: str
    author: str
    state: str
    comments: List[Dict[str, str]] = field(default_factory=list)
    review_comments: List[Dict[str, str]] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    merged_at: Optional[str] = None
    labels: List[str] = field(default_factory=list)


@dataclass
class OwnershipMap:
    file_path: str
    primary_owner: str
    contributors: List[Dict[str, Any]] = field(default_factory=list)
    last_modified: str = ""
    change_frequency: int = 0


def extract_blame(file_path: str, repo_path: str) -> List[BlameEntry]:
    """Run git blame on a file and extract ownership information."""
    full_path = Path(repo_path) / file_path
    if not full_path.exists():
        return []

    try:
        result = subprocess.run(
            ["git", "blame", "--porcelain", file_path],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    entries: List[BlameEntry] = []
    current_sha = ""
    current_author = ""
    current_email = ""
    current_date = ""
    current_line = 0

    for line in result.stdout.splitlines():
        # Header line: SHA original_line final_line [num_lines]
        sha_match = re.match(r'^([0-9a-f]{40}) \d+ (\d+)', line)
        if sha_match:
            current_sha = sha_match.group(1)
            current_line = int(sha_match.group(2))
            continue

        if line.startswith("author "):
            current_author = line[7:]
        elif line.startswith("author-mail "):
            current_email = line[12:].strip("<>")
        elif line.startswith("author-time "):
            ts = int(line[12:])
            current_date = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        elif line.startswith("\t"):
            entries.append(BlameEntry(
                file_path=file_path,
                author=current_author,
                email=current_email,
                date=current_date,
                line_start=current_line,
                line_end=current_line,
                commit_sha=current_sha,
            ))

    return entries


def compute_ownership(file_path: str, repo_path: str) -> Optional[OwnershipMap]:
    """Compute ownership map for a file based on git blame."""
    entries = extract_blame(file_path, repo_path)
    if not entries:
        return None

    author_lines: Dict[str, int] = {}
    for entry in entries:
        author_lines[entry.author] = author_lines.get(entry.author, 0) + 1

    total_lines = sum(author_lines.values())
    sorted_authors = sorted(author_lines.items(), key=lambda x: x[1], reverse=True)

    contributors = [
        {"author": author, "lines": lines, "pct": round(lines / max(total_lines, 1) * 100, 1)}
        for author, lines in sorted_authors
    ]

    # Get change frequency
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "--follow", file_path],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        change_frequency = len(log_result.stdout.strip().splitlines()) if log_result.returncode == 0 else 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        change_frequency = 0

    last_modified = entries[0].date if entries else ""

    return OwnershipMap(
        file_path=file_path,
        primary_owner=sorted_authors[0][0] if sorted_authors else "unknown",
        contributors=contributors,
        last_modified=last_modified,
        change_frequency=change_frequency,
    )


def fetch_pr_discussions(
    repo_path: str,
    limit: int = 20,
    state: str = "merged",
) -> List[PRDiscussion]:
    """Fetch PR discussions using GitHub CLI (gh)."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", state, "--limit", str(limit), "--json",
             "number,title,body,author,state,comments,reviewComments,files,mergedAt,labels"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode != 0:
        return []

    try:
        prs_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    discussions: List[PRDiscussion] = []
    for pr in prs_data:
        comments = [
            {"author": c.get("author", {}).get("login", ""), "body": c.get("body", "")}
            for c in pr.get("comments", [])
            if c.get("body")
        ]
        review_comments = [
            {"author": c.get("author", {}).get("login", ""), "body": c.get("body", ""), "path": c.get("path", "")}
            for c in pr.get("reviewComments", [])
            if c.get("body")
        ]
        files = [f.get("path", "") for f in pr.get("files", []) if f.get("path")]
        labels = [l.get("name", "") for l in pr.get("labels", []) if l.get("name")]

        discussions.append(PRDiscussion(
            pr_number=pr.get("number", 0),
            title=pr.get("title", ""),
            body=pr.get("body", ""),
            author=pr.get("author", {}).get("login", ""),
            state=pr.get("state", ""),
            comments=comments,
            review_comments=review_comments,
            files_changed=files,
            merged_at=pr.get("mergedAt"),
            labels=labels,
        ))

    return discussions


def ingest_git_knowledge(
    repo_path: str,
    knowledge_db,
    workspace: str,
    max_prs: int = 30,
    key_files: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Ingest git blame + PR discussions into the knowledge base.

    Returns counts of indexed entries.
    """
    stats = {"ownership_entries": 0, "pr_entries": 0, "errors": 0}

    # Ownership for key files
    if key_files:
        for file_path in key_files:
            try:
                ownership = compute_ownership(file_path, repo_path)
                if ownership:
                    entry_id = str(uuid.uuid4())
                    content = (
                        f"File: {ownership.file_path}\n"
                        f"Primary owner: {ownership.primary_owner}\n"
                        f"Change frequency: {ownership.change_frequency} commits\n"
                        f"Contributors: {json.dumps(ownership.contributors[:5])}"
                    )
                    knowledge_db.execute(
                        """INSERT OR REPLACE INTO knowledge_entries
                           (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
                           VALUES (?, ?, ?, 'git_blame', 'ownership', ?, ?, ?, ?)""",
                        (
                            entry_id, workspace, file_path,
                            f"Ownership: {file_path}",
                            content,
                            f"blame:{file_path}",
                            json.dumps({"primary_owner": ownership.primary_owner, "change_frequency": ownership.change_frequency}),
                        ),
                    )
                    stats["ownership_entries"] += 1
            except Exception:
                stats["errors"] += 1

    # PR discussions
    try:
        prs = fetch_pr_discussions(repo_path, limit=max_prs)
        for pr in prs:
            if not pr.body and not pr.comments:
                continue

            entry_id = str(uuid.uuid4())
            discussion_text = f"PR #{pr.pr_number}: {pr.title}\n\n{pr.body or ''}\n"
            for comment in pr.comments[:10]:
                discussion_text += f"\n@{comment['author']}: {comment['body'][:500]}"
            for rc in pr.review_comments[:10]:
                discussion_text += f"\n[{rc.get('path', '')}] @{rc['author']}: {rc['body'][:300]}"

            knowledge_db.execute(
                """INSERT OR REPLACE INTO knowledge_entries
                   (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
                   VALUES (?, ?, ?, 'github_pr', 'pr_discussion', ?, ?, ?, ?)""",
                (
                    entry_id, workspace, f"pr/{pr.pr_number}",
                    f"PR #{pr.pr_number}: {pr.title}",
                    discussion_text[:50000],
                    f"pr:{pr.pr_number}",
                    json.dumps({"files": pr.files_changed[:20], "author": pr.author, "labels": pr.labels}),
                ),
            )
            stats["pr_entries"] += 1
    except Exception:
        stats["errors"] += 1

    if stats["ownership_entries"] + stats["pr_entries"] > 0:
        knowledge_db.commit()

    return stats

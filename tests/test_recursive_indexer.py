"""Tests for cap.lib.recursive_indexer — recursive directory indexing."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def knowledge_db(tmp_path):
    """Create a minimal knowledge.db for testing."""
    db_path = tmp_path / "data" / "knowledge.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL,
            source_path TEXT,
            source_type TEXT NOT NULL,
            content_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            metadata TEXT,
            embedding_status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ke_workspace ON knowledge_entries(workspace);
        CREATE INDEX IF NOT EXISTS idx_ke_source_path ON knowledge_entries(source_path);

        CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
            id TEXT PRIMARY KEY,
            entity_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            workspace TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kgn_entity
            ON knowledge_graph_nodes(entity_name, entity_type, workspace);

        CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
            target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
            predicate TEXT NOT NULL,
            workspace TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source_id, target_id, predicate)
        );
        CREATE INDEX IF NOT EXISTS idx_kge_source ON knowledge_graph_edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_kge_target ON knowledge_graph_edges(target_id);
    """)
    conn.commit()
    conn.close()
    return tmp_path / "data"


@pytest.fixture
def sample_workspace(tmp_path):
    """Create a sample workspace with various file types."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Python files
    (ws / "src").mkdir()
    (ws / "src" / "main.py").write_text("print('hello world')\n")
    (ws / "src" / "utils.py").write_text("def helper():\n    return 42\n")

    # TypeScript
    (ws / "src" / "app.ts").write_text("const x: number = 1;\n")

    # Config files
    (ws / "config.yaml").write_text("key: value\n")
    (ws / "settings.json").write_text('{"debug": true}\n')

    # Documentation
    (ws / "README.md").write_text("# My Project\n\nA test project.\n")

    # Terraform
    (ws / "infra").mkdir()
    (ws / "infra" / "main.tf").write_text('resource "aws_s3_bucket" "test" {}\n')

    # Shell script
    (ws / "deploy.sh").write_text("#!/bin/bash\necho deploy\n")

    # Dockerfile
    (ws / "Dockerfile").write_text("FROM python:3.11\nCOPY . /app\n")

    # Makefile
    (ws / "Makefile").write_text("all:\n\techo build\n")

    return ws


@pytest.fixture
def workspace_with_exclusions(tmp_path):
    """Create a workspace with directories that should be excluded."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Normal file
    (ws / "main.py").write_text("# main\n")

    # Excluded directories
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "dep.js").write_text("module.exports = {};\n")

    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00")

    (ws / "dist").mkdir()
    (ws / "dist" / "bundle.js").write_text("// bundled\n")

    (ws / ".terraform").mkdir()
    (ws / ".terraform" / "lock.hcl").write_text("# lock\n")

    (ws / "vendor").mkdir()
    (ws / "vendor" / "lib.go").write_text("package lib\n")

    (ws / "build").mkdir()
    (ws / "build" / "output.js").write_text("// output\n")

    (ws / "venv").mkdir()
    (ws / "venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")

    return ws


@pytest.fixture
def workspace_with_git(tmp_path):
    """Create a workspace that looks like a git repo."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Create .git directory (fake — just for detection)
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("# app code\n")
    (ws / "README.md").write_text("# Repo\n")

    return ws


class TestDirectoryScanning:
    """Tests for basic directory scanning with exclusions."""

    def test_indexes_all_matching_files(self, sample_workspace, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(sample_workspace),
        }
        stats = index_directory_tree(sample_workspace, config)

        # Should index: main.py, utils.py, app.ts, config.yaml, settings.json,
        # README.md, main.tf, deploy.sh, Dockerfile, Makefile
        assert stats["files_indexed"] == 10
        assert stats["files_scanned"] > 0
        assert stats["duration_s"] >= 0

    def test_excludes_directories(self, workspace_with_exclusions, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(workspace_with_exclusions),
        }
        stats = index_directory_tree(workspace_with_exclusions, config)

        # Only main.py should be indexed (all others are in excluded dirs)
        assert stats["files_indexed"] == 1

    def test_custom_exclude_patterns(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "keep.py").write_text("# keep\n")
        (ws / "mydir").mkdir()
        (ws / "mydir" / "skip.py").write_text("# skip\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "exclude_dirs": {"mydir"},
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 1


class TestExtensionFiltering:
    """Tests for file extension filtering."""

    def test_only_indexes_matching_extensions(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "code.py").write_text("# python\n")
        (ws / "data.csv").write_text("a,b,c\n")  # Not in default extensions
        (ws / "image.png").write_bytes(b"\x89PNG")  # Binary, not in extensions
        (ws / "script.sh").write_text("#!/bin/bash\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)

        # Only .py and .sh should be indexed
        assert stats["files_indexed"] == 2

    def test_custom_extensions(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "code.py").write_text("# python\n")
        (ws / "data.csv").write_text("a,b,c\n")
        (ws / "notes.txt").write_text("notes\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "extensions": {".csv", ".txt"},  # Only index csv and txt
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 2

    def test_exact_filenames(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "Dockerfile").write_text("FROM alpine\n")
        (ws / "Makefile").write_text("all:\n\techo hi\n")
        (ws / "randomfile").write_text("not indexed\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "extensions": set(),  # No extensions — only exact filenames
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 2


class TestMaxFileSize:
    """Tests for max file size filtering."""

    def test_skips_files_exceeding_max_size(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "small.py").write_text("# small\n")
        (ws / "large.py").write_text("x" * (600 * 1024))  # 600 KB > default 500 KB

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 1
        assert stats["files_skipped_size"] == 1

    def test_custom_max_file_size(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "small.py").write_text("# small\n")  # < 1 KB
        (ws / "medium.py").write_text("x" * 2048)  # 2 KB

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "max_file_size_kb": 1,  # Only allow up to 1 KB
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 1
        assert stats["files_skipped_size"] == 1

    def test_empty_files_skipped(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "empty.py").write_text("")
        (ws / "notempty.py").write_text("# has content\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 1


class TestGitRepoDetection:
    """Tests for git repository detection."""

    def test_detects_git_repo(self, workspace_with_git, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(workspace_with_git),
        }
        stats = index_directory_tree(workspace_with_git, config)
        assert stats["repos_detected"] == 1

    def test_does_not_index_git_dir_contents(self, workspace_with_git, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(workspace_with_git),
        }
        stats = index_directory_tree(workspace_with_git, config)

        # .git/config and .git/HEAD should NOT be indexed
        conn = sqlite3.connect(str(knowledge_db / "knowledge.db"))
        results = conn.execute(
            "SELECT source_path FROM knowledge_entries WHERE source_path LIKE '%/.git/%'"
        ).fetchall()
        conn.close()
        assert len(results) == 0

    def test_creates_graph_nodes_for_repos(self, workspace_with_git, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(workspace_with_git),
        }
        stats = index_directory_tree(workspace_with_git, config)
        assert stats["graph_nodes_created"] > 0

        conn = sqlite3.connect(str(knowledge_db / "knowledge.db"))
        repos = conn.execute(
            "SELECT entity_name FROM knowledge_graph_nodes WHERE entity_type = 'repository'"
        ).fetchall()
        conn.close()
        assert len(repos) == 1

    def test_multiple_repos_detected(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()

        for name in ["repo-a", "repo-b", "repo-c"]:
            repo = ws / name
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / "main.py").write_text(f"# {name}\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)
        assert stats["repos_detected"] == 3


class TestBatchSizing:
    """Tests for batch insert behavior."""

    def test_batch_size_respected(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()

        # Create more files than batch size
        for i in range(15):
            (ws / f"file_{i:03d}.py").write_text(f"# file {i}\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "batch_size": 5,  # Small batch size
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 15

    def test_partial_batch_committed(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()

        # Create 3 files (less than default batch size of 100)
        for i in range(3):
            (ws / f"file_{i}.py").write_text(f"# file {i}\n")

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)
        assert stats["files_indexed"] == 3

        # Verify they're actually in the database
        conn = sqlite3.connect(str(knowledge_db / "knowledge.db"))
        count = conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]
        conn.close()
        assert count == 3


class TestSymlinksAndPermissions:
    """Tests for safe symlink and permission handling."""

    def test_does_not_follow_symlinks(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "real.py").write_text("# real\n")

        # Create a symlink loop
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("# secret outside workspace\n")
        (ws / "link").symlink_to(outside)

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        stats = index_directory_tree(ws, config)
        # Should only index real.py, not follow symlink
        assert stats["files_indexed"] == 1

    def test_handles_permission_errors_gracefully(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "readable.py").write_text("# readable\n")

        unreadable = ws / "unreadable.py"
        unreadable.write_text("# unreadable\n")
        os.chmod(str(unreadable), 0o000)

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
        }
        try:
            stats = index_directory_tree(ws, config)
            # Should index readable.py and skip unreadable.py
            assert stats["files_indexed"] == 1
            assert stats["files_skipped_permission"] >= 1
        finally:
            # Restore permissions for cleanup
            os.chmod(str(unreadable), 0o644)


class TestProgressCallback:
    """Tests for progress callback."""

    def test_progress_callback_called(self, tmp_path, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        for i in range(5):
            (ws / f"file_{i}.py").write_text(f"# {i}\n")

        progress_messages = []

        def callback(msg, done, total):
            progress_messages.append((msg, done, total))

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(ws),
            "batch_size": 2,
        }
        index_directory_tree(ws, config, progress_callback=callback)
        assert len(progress_messages) > 0
        # Last message should indicate completion
        assert "Complete" in progress_messages[-1][0]


class TestIdempotency:
    """Tests that re-indexing doesn't create duplicates."""

    def test_already_indexed_files_skipped(self, sample_workspace, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {
            "data_dir": str(knowledge_db),
            "workspace": str(sample_workspace),
        }
        stats1 = index_directory_tree(sample_workspace, config)
        assert stats1["files_indexed"] > 0

        # Run again — should find all files already indexed
        stats2 = index_directory_tree(sample_workspace, config)
        assert stats2["files_indexed"] == 0
        assert stats2["files_already_indexed"] == stats1["files_indexed"]


class TestErrorHandling:
    """Tests for error handling."""

    def test_raises_on_nonexistent_root(self, knowledge_db):
        from cap.lib.recursive_indexer import index_directory_tree

        config = {"data_dir": str(knowledge_db)}
        with pytest.raises(ValueError, match="not a directory"):
            index_directory_tree("/nonexistent/path/xyz", config)

    def test_raises_on_missing_knowledge_db(self, tmp_path):
        from cap.lib.recursive_indexer import index_directory_tree

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "test.py").write_text("# test\n")

        config = {"data_dir": str(tmp_path / "nodata")}
        with pytest.raises(FileNotFoundError, match="knowledge.db not found"):
            index_directory_tree(ws, config)

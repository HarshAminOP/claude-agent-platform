"""Tests for directory structure indexing and repo summary generation in sync_engine.py."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.db_init import init_knowledge_db
from cap.lib.sync_engine import sync_workspace, SyncStats


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    conn = init_knowledge_db(tmp_path)
    yield conn
    conn.close()


@pytest.fixture
def workspace_with_modules(tmp_path):
    """Workspace containing one repo with a modules/ dir holding tf subdirs."""
    repo = tmp_path / "okta-infra"
    repo.mkdir()
    (repo / ".git").mkdir()

    modules = repo / "modules"
    modules.mkdir()

    for mod_name, files in [
        ("antitrust", ["main.tf", "variables.tf", "outputs.tf"]),
        ("dev_teams", ["main.tf", "variables.tf", "outputs.tf"]),
        ("functional_github_teams", ["main.tf"]),
        ("opensearch_saml_app", ["main.tf", "variables.tf"]),
        ("ssm_share", ["main.tf"]),
    ]:
        mod_dir = modules / mod_name
        mod_dir.mkdir()
        for fname in files:
            (mod_dir / fname).write_text(f'# {mod_name}/{fname}\nresource "aws_iam_role" "{mod_name}" {{}}\n')

    return str(tmp_path)


@pytest.fixture
def workspace_minimal_dirs(tmp_path):
    """Workspace with a repo that has only 1-2 subdirs (should NOT become structural)."""
    repo = tmp_path / "small-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    sparse = repo / "sparse"
    sparse.mkdir()
    (sparse / "only_child").mkdir()
    (sparse / "only_child" / "main.tf").write_text('resource "aws_s3_bucket" "b" {}')

    return str(tmp_path)


# ── Test: structure entry is created ─────────────────────────────────────────

class TestIndexDirectoryStructures:

    def test_detects_modules_dir(self, db, workspace_with_modules):
        sync_workspace(db, workspace_with_modules)
        rows = db.execute(
            "SELECT title FROM knowledge_entries WHERE title LIKE '%(structure)'"
        ).fetchall()
        titles = [r[0] for r in rows]
        assert any("modules" in t for t in titles), f"No modules structure entry found. Titles: {titles}"

    def test_counts_files_in_content(self, db, workspace_with_modules):
        sync_workspace(db, workspace_with_modules)
        row = db.execute(
            "SELECT content FROM knowledge_entries WHERE source_path LIKE '%modules:structure'"
        ).fetchone()
        assert row is not None, "No structure entry for modules/"
        content = row[0]
        # antitrust has 3 files — should list them by name
        assert "antitrust/" in content
        assert "main.tf" in content

    def test_large_subdir_shows_count_only(self, db, tmp_path):
        """Subdirs with >5 files should show count, not list all filenames."""
        repo = tmp_path / "big-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        modules = repo / "modules"
        modules.mkdir()

        big_mod = modules / "big_module"
        big_mod.mkdir()
        for i in range(8):
            (big_mod / f"file_{i}.tf").write_text(f'resource "r" "r{i}" {{}}')

        # Need enough sibling dirs to trigger structural detection
        for i in range(3):
            sib = modules / f"sib_{i}"
            sib.mkdir()
            (sib / "main.tf").write_text('resource "r" "r" {}')

        sync_workspace(db, str(tmp_path))
        row = db.execute(
            "SELECT content FROM knowledge_entries WHERE source_path LIKE '%modules:structure'"
        ).fetchone()
        assert row is not None
        content = row[0]
        assert "big_module/ (8 files)" in content
        # Individual file names should NOT be listed for big_module
        assert "file_0.tf" not in content

    def test_structure_entry_is_idempotent(self, db, workspace_with_modules):
        """Running sync twice should not create duplicate structure entries."""
        sync_workspace(db, workspace_with_modules)
        sync_workspace(db, workspace_with_modules)
        rows = db.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE source_path LIKE '%modules:structure'"
        ).fetchone()
        assert rows[0] == 1, f"Expected 1 structure entry, got {rows[0]}"

    def test_skips_non_structural_dirs(self, db, workspace_minimal_dirs):
        sync_workspace(db, workspace_minimal_dirs)
        rows = db.execute(
            "SELECT title FROM knowledge_entries WHERE title LIKE '%(structure)'"
        ).fetchall()
        titles = [r[0] for r in rows]
        assert not titles, f"Unexpected structure entries for sparse dirs: {titles}"


# ── Test: repo summary ────────────────────────────────────────────────────────

class TestRepoSummary:

    def test_repo_summary_entry_created(self, db, workspace_with_modules):
        sync_workspace(db, workspace_with_modules)
        row = db.execute(
            "SELECT title FROM knowledge_entries WHERE source_path = 'okta-infra:summary'"
        ).fetchone()
        assert row is not None, "No repo summary entry for okta-infra"
        assert "okta-infra (repo summary)" == row[0]

    def test_repo_summary_includes_module_names(self, db, workspace_with_modules):
        sync_workspace(db, workspace_with_modules)
        row = db.execute(
            "SELECT content FROM knowledge_entries WHERE source_path = 'okta-infra:summary'"
        ).fetchone()
        assert row is not None
        content = row[0]
        assert "antitrust" in content
        assert "dev_teams" in content
        assert "ssm_share" in content

    def test_repo_summary_idempotent(self, db, workspace_with_modules):
        sync_workspace(db, workspace_with_modules)
        sync_workspace(db, workspace_with_modules)
        rows = db.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE source_path = 'okta-infra:summary'"
        ).fetchone()
        assert rows[0] == 1, f"Expected 1 summary entry, got {rows[0]}"


# ── Test: SyncStats ───────────────────────────────────────────────────────────

class TestSyncStats:

    def test_stats_track_structures(self, db, workspace_with_modules):
        stats = sync_workspace(db, workspace_with_modules)
        assert stats.structures_indexed >= 1

    def test_stats_track_repo_summaries(self, db, workspace_with_modules):
        stats = sync_workspace(db, workspace_with_modules)
        assert stats.repo_summaries_generated >= 1

    def test_new_stats_fields_default_zero(self):
        s = SyncStats()
        assert s.structures_indexed == 0
        assert s.repo_summaries_generated == 0

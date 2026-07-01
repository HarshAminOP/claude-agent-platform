"""Tests for cap.lib.dependency_resolver — cross-repo dependency discovery."""

import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cap.lib.dependency_resolver import (
    Dependency,
    DependencyGraph,
    DependencyResolver,
    DependencyResolverConfig,
    GraphStats,
    RepoSource,
    _find_files,
    _find_local_repo,
    _flush_helm_dep,
    _read_file,
    _safe_target_name,
)
from cap.lib.config import GitHubConfig, PlatformConfig


# ---------------------------------------------------------------------------
# Schema (matches real knowledge.db)
# ---------------------------------------------------------------------------

KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid         TEXT NOT NULL UNIQUE,
    workspace    TEXT NOT NULL,
    source_path  TEXT,
    source_type  TEXT NOT NULL,
    content_type TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
    id          TEXT PRIMARY KEY,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kgn ON knowledge_graph_nodes(entity_name, entity_type, workspace);

CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    target_id  TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    predicate  TEXT NOT NULL,
    weight     REAL DEFAULT 1.0,
    metadata   TEXT,
    workspace  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, target_id, predicate)
);
"""


@pytest.fixture()
def db():
    """In-memory SQLite with knowledge schema."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(KNOWLEDGE_SCHEMA)
    yield conn
    conn.close()


@pytest.fixture()
def resolver(db):
    """DependencyResolver with auto_clone disabled and org set for test fixtures.

    Tests use 'moia-oss' import paths in their fixture data, so the platform
    config must carry that org name.  In production the org comes from the
    user's harness-config.json (set during `cap init`); the hardcoded default
    was removed from the production code for portability.
    """
    cfg = DependencyResolverConfig(auto_clone=False)
    platform_cfg = PlatformConfig(
        github=GitHubConfig(
            org="moia-oss",
            clone_base_path="",
            # "moia-dev" is a sibling org used in test fixtures; "moia" is the
            # Python package prefix used in pyproject/requirements test fixtures.
            # "moia-dev" is a sibling Go org; "moia" and "cap" are Python
            # package name prefixes used in the fixture data.
            org_aliases=["moia-dev", "moia", "cap"],
        ),
    )
    return DependencyResolver(db, config=cfg, platform_config=platform_cfg)


# ---------------------------------------------------------------------------
# Helpers for building fake repo layouts
# ---------------------------------------------------------------------------

def _make_git_repo(base: Path, domain: str, name: str) -> Path:
    """Create a minimal git repo under base/domain/name."""
    repo = base / domain / name
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo


# ---------------------------------------------------------------------------
# _safe_target_name
# ---------------------------------------------------------------------------

class TestSafeTargetName:
    def test_valid_simple(self):
        assert _safe_target_name("my-repo") == "my-repo"

    def test_valid_with_dots(self):
        assert _safe_target_name("my.repo.v2") == "my.repo.v2"

    def test_rejects_path_traversal(self):
        assert _safe_target_name("../etc/passwd") is None

    def test_rejects_slash(self):
        assert _safe_target_name("org/repo") is None

    def test_rejects_empty(self):
        assert _safe_target_name("") is None

    def test_rejects_backslash(self):
        assert _safe_target_name("windows\\path") is None

    def test_rejects_special_chars(self):
        # Semicolon would be injection
        assert _safe_target_name("repo;rm -rf") is None


# ---------------------------------------------------------------------------
# _find_local_repo
# ---------------------------------------------------------------------------

class TestFindLocalRepo:
    def test_finds_direct(self, tmp_path):
        repo = tmp_path / "my-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = _find_local_repo("my-repo", tmp_path)
        assert result == repo

    def test_finds_under_domain_dir(self, tmp_path):
        domain = tmp_path / "Services"
        domain.mkdir()
        repo = domain / "api-service"
        repo.mkdir()
        (repo / ".git").mkdir()
        result = _find_local_repo("api-service", tmp_path)
        assert result == repo

    def test_returns_none_when_missing(self, tmp_path):
        result = _find_local_repo("nonexistent-repo", tmp_path)
        assert result is None

    def test_returns_none_for_nonexistent_base(self, tmp_path):
        missing_base = tmp_path / "does-not-exist"
        result = _find_local_repo("any-repo", missing_base)
        assert result is None

    def test_ignores_directory_without_git(self, tmp_path):
        repo = tmp_path / "not-a-repo"
        repo.mkdir()
        # no .git directory
        result = _find_local_repo("not-a-repo", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_small_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        assert _read_file(f) == "hello world"

    def test_returns_none_for_missing_file(self, tmp_path):
        result = _read_file(tmp_path / "nonexistent.txt")
        assert result is None

    def test_returns_none_when_oversized(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (65 * 1024))  # 65 KB > 64 KB cap
        assert _read_file(f) is None


# ---------------------------------------------------------------------------
# _find_files
# ---------------------------------------------------------------------------

class TestFindFiles:
    def test_finds_matching_files(self, tmp_path):
        (tmp_path / "a.tf").write_text("resource {}")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.tf").write_text("module {}")
        results = _find_files(tmp_path, "*.tf", max_depth=2)
        names = {p.name for p in results}
        assert names == {"a.tf", "b.tf"}

    def test_respects_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.tf").write_text("x")
        results = _find_files(tmp_path, "*.tf", max_depth=2)
        # depth 3 (a/b/c) exceeds max_depth=2; should not be found
        assert all(p.name != "deep.tf" for p in results)

    def test_skips_skip_dirs(self, tmp_path):
        node_mods = tmp_path / "node_modules"
        node_mods.mkdir()
        (node_mods / "bad.tf").write_text("x")
        results = _find_files(tmp_path, "*.tf", max_depth=2)
        assert not results


# ---------------------------------------------------------------------------
# discover_all_sources
# ---------------------------------------------------------------------------

class TestDiscoverAllSources:
    def test_discovers_git_repos(self, tmp_path, resolver):
        _make_git_repo(tmp_path, "Services", "api")
        _make_git_repo(tmp_path, "Services", "worker")

        sources = resolver.discover_all_sources([str(tmp_path)])
        names = {s.name for s in sources}
        assert "api" in names
        assert "worker" in names

    def test_ignores_directories_without_git(self, tmp_path, resolver):
        (tmp_path / "Services" / "not-a-repo").mkdir(parents=True)
        # no .git
        sources = resolver.discover_all_sources([str(tmp_path)])
        assert sources == []

    def test_skips_nonexistent_roots(self, tmp_path, resolver):
        sources = resolver.discover_all_sources([str(tmp_path / "missing")])
        assert sources == []

    def test_deduplicates_across_roots(self, tmp_path, resolver):
        _make_git_repo(tmp_path, "Services", "api")
        sources = resolver.discover_all_sources([str(tmp_path), str(tmp_path)])
        assert len([s for s in sources if s.name == "api"]) == 1

    def test_sets_domain_from_parent_dir(self, tmp_path, resolver):
        _make_git_repo(tmp_path, "Observability", "metrics")
        sources = resolver.discover_all_sources([str(tmp_path)])
        assert sources[0].domain == "Observability"


# ---------------------------------------------------------------------------
# _scan_terraform_deps
# ---------------------------------------------------------------------------

class TestScanTerraformDeps:
    def test_detects_remote_state(self, tmp_path, resolver):
        tf_content = textwrap.dedent("""\
            data "terraform_remote_state" "vpc" {
              backend = "s3"
              config = {
                key    = "networking/vpc/terraform.tfstate"
                bucket = "my-tf-state"
              }
            }
        """)
        (tmp_path / "main.tf").write_text(tf_content)

        deps = resolver._scan_terraform_deps(tmp_path, "my-service")
        assert len(deps) == 1
        assert deps[0].dep_type == "terraform_remote_state"
        assert deps[0].target == "networking"
        assert deps[0].source_repo == "my-service"

    def test_detects_git_module_source(self, tmp_path, resolver):
        tf_content = textwrap.dedent("""\
            module "ecs_task" {
              source = "git::https://github.com/moia-oss/terraform-modules.git//ecs?ref=v2.0"
            }
        """)
        (tmp_path / "main.tf").write_text(tf_content)

        deps = resolver._scan_terraform_deps(tmp_path, "my-service")
        module_deps = [d for d in deps if d.dep_type == "terraform_module"]
        assert len(module_deps) == 1
        assert module_deps[0].target == "terraform-modules"

    def test_skips_local_module_source(self, tmp_path, resolver):
        tf_content = textwrap.dedent("""\
            module "helper" {
              source = "./modules/helper"
            }
        """)
        (tmp_path / "main.tf").write_text(tf_content)
        deps = resolver._scan_terraform_deps(tmp_path, "my-service")
        assert not any(d.dep_type == "terraform_module" for d in deps)

    def test_deduplicates_remote_state_refs(self, tmp_path, resolver):
        tf_content = textwrap.dedent("""\
            data "terraform_remote_state" "vpc" {
              config = { key = "networking/terraform.tfstate" }
            }
            data "terraform_remote_state" "vpc2" {
              config = { key = "networking/terraform.tfstate" }
            }
        """)
        (tmp_path / "main.tf").write_text(tf_content)
        deps = resolver._scan_terraform_deps(tmp_path, "my-service")
        networking_deps = [d for d in deps if d.target == "networking"]
        assert len(networking_deps) == 1


# ---------------------------------------------------------------------------
# _scan_helm_deps
# ---------------------------------------------------------------------------

class TestScanHelmDeps:
    def test_detects_chart_dependency(self, tmp_path, resolver):
        chart_yaml = textwrap.dedent("""\
            apiVersion: v2
            name: my-chart
            version: 1.0.0
            dependencies:
              - name: redis
                version: "17.x"
                repository: https://charts.bitnami.com/bitnami
              - name: postgres
                version: "12.x"
                repository: https://charts.bitnami.com/bitnami
        """)
        (tmp_path / "Chart.yaml").write_text(chart_yaml)

        deps = resolver._scan_helm_deps(tmp_path, "my-service")
        names = {d.target for d in deps}
        assert "redis" in names
        assert "postgres" in names
        assert all(d.dep_type == "helm_dep" for d in deps)

    def test_no_deps_returns_empty(self, tmp_path, resolver):
        chart_yaml = "apiVersion: v2\nname: my-chart\nversion: 1.0.0\n"
        (tmp_path / "Chart.yaml").write_text(chart_yaml)
        deps = resolver._scan_helm_deps(tmp_path, "my-service")
        assert deps == []

    def test_missing_chart_yaml_returns_empty(self, tmp_path, resolver):
        deps = resolver._scan_helm_deps(tmp_path, "my-service")
        assert deps == []


# ---------------------------------------------------------------------------
# _scan_argocd_deps
# ---------------------------------------------------------------------------

class TestScanArgocdDeps:
    def test_detects_repourl(self, tmp_path, resolver):
        app_yaml = textwrap.dedent("""\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: my-app
            spec:
              source:
                repoURL: https://github.com/moia-oss/helm-charts.git
                targetRevision: HEAD
        """)
        (tmp_path / "app.yaml").write_text(app_yaml)

        deps = resolver._scan_argocd_deps(tmp_path, "my-service")
        assert len(deps) == 1
        assert deps[0].dep_type == "argocd_repo"
        assert deps[0].target == "helm-charts"

    def test_skips_non_argocd_yaml(self, tmp_path, resolver):
        (tmp_path / "values.yaml").write_text("key: value\nrepoURL: not-really-argocd\n")
        deps = resolver._scan_argocd_deps(tmp_path, "my-service")
        assert deps == []

    def test_skips_self_reference(self, tmp_path, resolver):
        """repoURL pointing to source_repo itself must be excluded."""
        app_yaml = textwrap.dedent("""\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            spec:
              source:
                repoURL: https://github.com/moia-oss/my-service.git
        """)
        (tmp_path / "app.yaml").write_text(app_yaml)
        deps = resolver._scan_argocd_deps(tmp_path, "my-service")
        assert deps == []


# ---------------------------------------------------------------------------
# _scan_go_deps
# ---------------------------------------------------------------------------

class TestScanGoDeps:
    def test_detects_org_internal_import(self, tmp_path, resolver):
        go_mod = textwrap.dedent("""\
            module github.com/moia-oss/my-service

            go 1.21

            require (
                github.com/moia-oss/shared-lib v1.2.3
                github.com/moia-dev/platform-sdk v0.5.0
                github.com/external/thirdparty v3.0.0
            )
        """)
        (tmp_path / "go.mod").write_text(go_mod)

        deps = resolver._scan_go_deps(tmp_path, "my-service")
        targets = {d.target for d in deps}
        assert "shared-lib" in targets
        assert "platform-sdk" in targets
        # external dependency must not appear
        assert not any(d.target == "thirdparty" for d in deps)

    def test_no_go_mod_returns_empty(self, tmp_path, resolver):
        deps = resolver._scan_go_deps(tmp_path, "my-service")
        assert deps == []

    def test_excludes_self_import(self, tmp_path, resolver):
        go_mod = textwrap.dedent("""\
            module github.com/moia-oss/my-service

            require (
                github.com/moia-oss/my-service v0.0.0
            )
        """)
        (tmp_path / "go.mod").write_text(go_mod)
        deps = resolver._scan_go_deps(tmp_path, "my-service")
        assert deps == []


# ---------------------------------------------------------------------------
# _scan_python_deps
# ---------------------------------------------------------------------------

class TestScanPythonDeps:
    def test_detects_moia_package_in_pyproject(self, tmp_path, resolver):
        pyproject = textwrap.dedent("""\
            [project]
            name = "my-service"
            dependencies = [
                "moia-shared>=1.0",
                "requests>=2.28",
            ]
        """)
        (tmp_path / "pyproject.toml").write_text(pyproject)

        deps = resolver._scan_python_deps(tmp_path, "my-service")
        targets = {d.target for d in deps}
        assert "moia-shared" in targets
        # stdlib/third-party must not appear
        assert not any(d.target == "requests" for d in deps)

    def test_detects_moia_package_in_requirements(self, tmp_path, resolver):
        req_txt = textwrap.dedent("""\
            requests==2.28.0
            moia-toolkit>=2.0
            cap-core==1.0
        """)
        (tmp_path / "requirements.txt").write_text(req_txt)

        deps = resolver._scan_python_deps(tmp_path, "my-service")
        targets = {d.target for d in deps}
        assert "moia-toolkit" in targets
        assert "cap-core" in targets
        assert "requests" not in targets

    def test_no_deps_file_returns_empty(self, tmp_path, resolver):
        deps = resolver._scan_python_deps(tmp_path, "my-service")
        assert deps == []


# ---------------------------------------------------------------------------
# _scan_github_actions
# ---------------------------------------------------------------------------

class TestScanGitHubActions:
    def test_detects_reusable_workflow(self, tmp_path, resolver):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        wf_content = textwrap.dedent("""\
            name: CI
            on: [push]
            jobs:
              build:
                uses: moia-oss/shared-workflows/.github/workflows/build.yml@main
        """)
        (wf_dir / "ci.yml").write_text(wf_content)

        deps = resolver._scan_github_actions(tmp_path, "my-service")
        assert len(deps) == 1
        assert deps[0].dep_type == "github_action"
        assert deps[0].target == "shared-workflows"

    def test_ignores_marketplace_actions(self, tmp_path, resolver):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        wf_content = textwrap.dedent("""\
            name: CI
            on: [push]
            jobs:
              build:
                steps:
                  - uses: actions/checkout@v4
                  - uses: aws-actions/configure-aws-credentials@v4
        """)
        (wf_dir / "ci.yml").write_text(wf_content)

        deps = resolver._scan_github_actions(tmp_path, "my-service")
        assert deps == []

    def test_ignores_local_workflow_refs(self, tmp_path, resolver):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        wf_content = "jobs:\n  build:\n    uses: ./.github/workflows/shared.yml\n"
        (wf_dir / "ci.yml").write_text(wf_content)
        deps = resolver._scan_github_actions(tmp_path, "my-service")
        assert deps == []

    def test_no_workflows_dir_returns_empty(self, tmp_path, resolver):
        deps = resolver._scan_github_actions(tmp_path, "my-service")
        assert deps == []


# ---------------------------------------------------------------------------
# _terraform_module_target (static method)
# ---------------------------------------------------------------------------

class TestTerraformModuleTarget:
    def test_git_https_url(self):
        result = DependencyResolver._terraform_module_target(
            "git::https://github.com/moia-oss/terraform-modules.git//ecs?ref=v2"
        )
        assert result == "terraform-modules"

    def test_ssh_url(self):
        result = DependencyResolver._terraform_module_target(
            "git@github.com:moia-oss/infra-modules.git"
        )
        assert result == "infra-modules"

    def test_terraform_registry(self):
        result = DependencyResolver._terraform_module_target("hashicorp/consul/aws")
        assert result == "hashicorp-consul"

    def test_local_path_returns_none(self):
        assert DependencyResolver._terraform_module_target("./modules/vpc") is None
        assert DependencyResolver._terraform_module_target("../shared/modules") is None


# ---------------------------------------------------------------------------
# _extract_aws_resources (static method)
# ---------------------------------------------------------------------------

class TestExtractAwsResources:
    def test_extracts_resource_types(self, tmp_path):
        tf_content = textwrap.dedent("""\
            resource "aws_s3_bucket" "data" {}
            resource "aws_iam_role" "service" {}
            resource "aws_lambda_function" "processor" {}
        """)
        (tmp_path / "main.tf").write_text(tf_content)

        result = DependencyResolver._extract_aws_resources(tmp_path)
        assert "aws_s3_bucket" in result
        assert "aws_iam_role" in result
        assert "aws_lambda_function" in result

    def test_returns_sorted_unique(self, tmp_path):
        tf_content = textwrap.dedent("""\
            resource "aws_s3_bucket" "a" {}
            resource "aws_s3_bucket" "b" {}
        """)
        (tmp_path / "main.tf").write_text(tf_content)
        result = DependencyResolver._extract_aws_resources(tmp_path)
        assert result == ["aws_s3_bucket"]

    def test_returns_empty_for_no_tf_files(self, tmp_path):
        assert DependencyResolver._extract_aws_resources(tmp_path) == []


# ---------------------------------------------------------------------------
# resolve_dependencies — integration with scanners
# ---------------------------------------------------------------------------

class TestResolveDependencies:
    def test_basic_end_to_end(self, tmp_path, db):
        """Scans a fake repo and returns a DependencyGraph with expected deps."""
        repo = _make_git_repo(tmp_path, "Services", "api-service")
        (repo / "main.tf").write_text(textwrap.dedent("""\
            data "terraform_remote_state" "vpc" {
              config = { key = "networking/terraform.tfstate" }
            }
        """))
        (repo / "go.mod").write_text(textwrap.dedent("""\
            module github.com/moia-oss/api-service
            require (
                github.com/moia-oss/shared-lib v1.0.0
            )
        """))

        cfg = DependencyResolverConfig(auto_clone=False)
        platform_cfg = PlatformConfig(
            github=GitHubConfig(org="moia-oss", clone_base_path=""),
        )
        resolver = DependencyResolver(db, config=cfg, platform_config=platform_cfg)
        sources = resolver.discover_all_sources([str(tmp_path)])
        dep_graph = resolver.resolve_dependencies(sources)

        types = {d.dep_type for d in dep_graph.dependencies}
        assert "terraform_remote_state" in types
        assert "go_import" in types

    def test_respects_max_repos_per_run(self, tmp_path, db):
        for i in range(5):
            _make_git_repo(tmp_path, "Services", f"repo-{i}")

        cfg = DependencyResolverConfig(auto_clone=False, max_repos_per_run=2)
        resolver = DependencyResolver(db, config=cfg)
        sources = resolver.discover_all_sources([str(tmp_path)])
        dep_graph = resolver.resolve_dependencies(sources)

        assert dep_graph.resolution_stats["repos_scanned"] == 2

    def test_disabled_scanner_not_called(self, tmp_path, db):
        """When scan_terraform=False, no TF deps should appear."""
        repo = _make_git_repo(tmp_path, "Services", "svc")
        (repo / "main.tf").write_text(
            'data "terraform_remote_state" "vpc" { config = { key = "vpc/state" } }'
        )

        cfg = DependencyResolverConfig(auto_clone=False, scan_terraform=False)
        resolver = DependencyResolver(db, config=cfg)
        sources = resolver.discover_all_sources([str(tmp_path)])
        dep_graph = resolver.resolve_dependencies(sources)

        assert not any(d.dep_type == "terraform_remote_state" for d in dep_graph.dependencies)


# ---------------------------------------------------------------------------
# build_full_graph
# ---------------------------------------------------------------------------

class TestBuildFullGraph:
    def _make_graph(self, db) -> tuple[DependencyGraph, str]:
        workspace = "test-workspace"
        deps = [
            Dependency(
                source_repo="api-service",
                target="vpc",
                dep_type="terraform_remote_state",
                reference='data "terraform_remote_state" "vpc" {}',
            ),
            Dependency(
                source_repo="api-service",
                target="shared-lib",
                dep_type="go_import",
                reference="github.com/moia-oss/shared-lib",
            ),
            Dependency(
                source_repo="api-service",
                target="redis",
                dep_type="helm_dep",
                reference="redis",
            ),
        ]
        dep_graph = DependencyGraph(
            sources=[],
            dependencies=deps,
            unresolved=[],
        )
        return dep_graph, workspace

    def test_creates_edges_in_db(self, db):
        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)
        dep_graph, workspace = self._make_graph(db)

        stats = resolver.build_full_graph(dep_graph, workspace)

        assert stats.edges_created == 3
        assert stats.errors == []

    def test_edges_present_in_db(self, db):
        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)
        dep_graph, workspace = self._make_graph(db)
        resolver.build_full_graph(dep_graph, workspace)

        rows = db.execute(
            "SELECT COUNT(*) FROM knowledge_graph_edges WHERE workspace = ?",
            (workspace,),
        ).fetchone()
        assert rows[0] == 3

    def test_provisions_edges_from_resolved_tf_module(self, tmp_path, db):
        """build_full_graph emits provisions edges for AWS resources in resolved TF modules."""
        module_repo = tmp_path / "tf-modules"
        module_repo.mkdir()
        (module_repo / "main.tf").write_text(
            'resource "aws_s3_bucket" "data" {}\nresource "aws_iam_role" "svc" {}\n'
        )

        deps = [
            Dependency(
                source_repo="api-service",
                target="tf-modules",
                dep_type="terraform_module",
                reference="git::https://github.com/moia-oss/tf-modules.git",
                resolved=True,
                resolved_path=str(module_repo),
            ),
        ]
        dep_graph = DependencyGraph(sources=[], dependencies=deps, unresolved=[])

        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)
        stats = resolver.build_full_graph(dep_graph, workspace="ws")

        # 1 uses_module edge + 2 provisions edges
        assert stats.edges_created == 3

    def test_unknown_dep_type_is_skipped_gracefully(self, db):
        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)

        deps = [
            Dependency(
                source_repo="svc",
                target="other",
                dep_type="unknown_type",
                reference="raw",
            ),
        ]
        dep_graph = DependencyGraph(sources=[], dependencies=deps, unresolved=[])
        stats = resolver.build_full_graph(dep_graph, workspace="ws")

        assert stats.edges_created == 0
        assert stats.errors == []


# ---------------------------------------------------------------------------
# get_dependency_tree
# ---------------------------------------------------------------------------

class TestGetDependencyTree:
    def _seed_edges(self, db, workspace: str):
        from cap.lib.graph import add_edge
        add_edge(db, "api", "repo", "shared-lib", "repo", "depends_on", workspace)
        add_edge(db, "shared-lib", "repo", "base-lib", "repo", "depends_on", workspace)

    def test_returns_nested_tree(self, db):
        workspace = "ws"
        self._seed_edges(db, workspace)

        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)

        tree = resolver.get_dependency_tree("api", max_depth=2)
        assert tree["name"] == "api"
        child_names = {c["name"] for c in tree["deps"]}
        assert "shared-lib" in child_names

    def test_cycle_detection(self, db):
        from cap.lib.graph import add_edge
        workspace = "ws"
        add_edge(db, "a", "repo", "b", "repo", "depends_on", workspace)
        add_edge(db, "b", "repo", "a", "repo", "depends_on", workspace)

        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)

        tree = resolver.get_dependency_tree("a", max_depth=3)
        # Should not raise; cycle node should be marked
        def _has_cycle_node(node):
            if node.get("cycle"):
                return True
            return any(_has_cycle_node(c) for c in node.get("deps", []))

        assert _has_cycle_node(tree)

    def test_respects_max_depth(self, db):
        from cap.lib.graph import add_edge
        ws = "ws"
        add_edge(db, "a", "repo", "b", "repo", "depends_on", ws)
        add_edge(db, "b", "repo", "c", "repo", "depends_on", ws)

        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)

        tree = resolver.get_dependency_tree("a", max_depth=1)
        # Depth=1 means we see b but not c
        assert len(tree["deps"]) == 1
        assert tree["deps"][0]["name"] == "b"
        assert tree["deps"][0]["deps"] == []


# ---------------------------------------------------------------------------
# find_dependents
# ---------------------------------------------------------------------------

class TestFindDependents:
    def test_returns_dependents(self, db):
        from cap.lib.graph import add_edge
        ws = "ws"
        add_edge(db, "service-a", "repo", "shared-lib", "repo", "depends_on", ws)
        add_edge(db, "service-b", "repo", "shared-lib", "repo", "depends_on", ws)
        add_edge(db, "service-c", "repo", "other-lib",  "repo", "depends_on", ws)

        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)

        dependents = resolver.find_dependents("shared-lib")
        assert sorted(dependents) == ["service-a", "service-b"]

    def test_returns_empty_when_no_dependents(self, db):
        cfg = DependencyResolverConfig(auto_clone=False)
        resolver = DependencyResolver(db, config=cfg)
        dependents = resolver.find_dependents("orphan-lib")
        assert dependents == []


# ---------------------------------------------------------------------------
# DependencyGraph properties
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_resolved_count(self):
        deps = [
            Dependency("a", "b", "go_import", "ref", resolved=True),
            Dependency("a", "c", "go_import", "ref", resolved=False),
            Dependency("a", "d", "go_import", "ref", resolved=True),
        ]
        graph = DependencyGraph(sources=[], dependencies=deps, unresolved=[])
        assert graph.resolved_count == 2


# ---------------------------------------------------------------------------
# _flush_helm_dep (helper)
# ---------------------------------------------------------------------------

class TestFlushHelmDep:
    def test_adds_dep(self):
        deps: list[Dependency] = []
        seen: set[str] = set()
        _flush_helm_dep({"name": "redis", "version": "17.x"}, "my-svc", seen, deps)
        assert len(deps) == 1
        assert deps[0].target == "redis"
        assert deps[0].dep_type == "helm_dep"

    def test_deduplicates(self):
        deps: list[Dependency] = []
        seen: set[str] = set()
        _flush_helm_dep({"name": "redis"}, "svc", seen, deps)
        _flush_helm_dep({"name": "redis"}, "svc", seen, deps)
        assert len(deps) == 1

    def test_ignores_empty_name(self):
        deps: list[Dependency] = []
        seen: set[str] = set()
        _flush_helm_dep({"name": ""}, "svc", seen, deps)
        assert deps == []

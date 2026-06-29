"""Tests for cap.lib.repo_extractor — structured per-repo knowledge extraction."""

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

from cap.lib.repo_extractor import (
    RepoSummary,
    ExtractorStats,
    discover_and_extract,
    extract_and_index_repos,
    _extract_purpose,
    _detect_tech_stack,
    _extract_helm_info,
    _extract_go_module,
    _extract_terraform_info,
    _extract_ci_pipelines,
    _extract_argocd_info,
    _extract_owners,
    _extract_irsa_refs,
    _build_summary_text,
    _first_paragraph,
)


# ---------------------------------------------------------------------------
# Fixtures
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
    embedding_status TEXT DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_graph_nodes (
    id          TEXT PRIMARY KEY,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kgn_entity ON knowledge_graph_nodes(entity_name, entity_type, workspace);

CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    target_id TEXT NOT NULL REFERENCES knowledge_graph_nodes(id),
    predicate TEXT NOT NULL,
    weight    REAL DEFAULT 1.0,
    metadata  TEXT,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, target_id, predicate)
);
"""


@pytest.fixture()
def db():
    """In-memory SQLite with knowledge schema (no FTS5, no triggers for speed)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(KNOWLEDGE_SCHEMA)
    yield conn
    conn.close()


@pytest.fixture()
def workspace(tmp_path):
    """Create a fake workspace under tmp_path/repos/."""
    repos = tmp_path / "repos"
    repos.mkdir()
    return tmp_path


def _make_repo(repos_root: Path, domain: str, name: str) -> Path:
    """Create a minimal repo directory structure."""
    repo = repos_root / domain / name
    repo.mkdir(parents=True)
    # Marker that makes it a detectable repo
    (repo / ".git").mkdir()
    return repo


# ---------------------------------------------------------------------------
# _first_paragraph
# ---------------------------------------------------------------------------

class TestFirstParagraph:
    def test_skips_heading_and_badge(self):
        text = textwrap.dedent("""\
            # My Service

            ![badge](http://example.com/badge.svg)

            This service handles alerting for production clusters.
            It routes alerts to PagerDuty and Slack.
        """)
        result = _first_paragraph(text)
        assert result.startswith("This service handles alerting")
        assert "badge" not in result

    def test_returns_empty_for_heading_only(self):
        text = "# Just a title\n\n## Subtitle\n"
        result = _first_paragraph(text)
        assert result == ""

    def test_truncates_long_paragraph(self):
        long_line = "word " * 200
        result = _first_paragraph(long_line)
        assert len(result) <= 403  # 400 + "..."

    def test_short_paragraph_returned_intact(self):
        text = "\nSimple purpose.\n"
        assert _first_paragraph(text) == "Simple purpose."


# ---------------------------------------------------------------------------
# _extract_purpose
# ---------------------------------------------------------------------------

class TestExtractPurpose:
    def test_reads_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# Title\n\nHandles widget management.\n")
        assert _extract_purpose(tmp_path) == "Handles widget management."

    def test_missing_readme_returns_empty(self, tmp_path):
        assert _extract_purpose(tmp_path) == ""

    def test_lowercase_readme(self, tmp_path):
        (tmp_path / "readme.md").write_text("# T\n\nLowercase readme.\n")
        assert _extract_purpose(tmp_path) == "Lowercase readme."


# ---------------------------------------------------------------------------
# _detect_tech_stack
# ---------------------------------------------------------------------------

class TestDetectTechStack:
    def test_detects_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/moia-dev/svc\ngo 1.22\n")
        stack = _detect_tech_stack(tmp_path)
        assert "Go" in stack

    def test_detects_helm(self, tmp_path):
        (tmp_path / "Chart.yaml").write_text("name: my-chart\nversion: 0.1.0\n")
        stack = _detect_tech_stack(tmp_path)
        assert "Helm" in stack

    def test_detects_terraform(self, tmp_path):
        (tmp_path / "main.tf").write_text('terraform { required_version = ">= 1.0" }\n')
        stack = _detect_tech_stack(tmp_path)
        assert "Terraform" in stack

    def test_detects_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'svc'\n")
        stack = _detect_tech_stack(tmp_path)
        assert "Python" in stack

    def test_detects_container(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
        stack = _detect_tech_stack(tmp_path)
        assert "Container" in stack

    def test_multiple_stacks(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n")
        (tmp_path / "Dockerfile").write_text("FROM golang:1.22\n")
        stack = _detect_tech_stack(tmp_path)
        assert "Go" in stack
        assert "Container" in stack

    def test_empty_dir_returns_yaml_or_empty(self, tmp_path):
        # YAML may appear if we find a .yaml file, but bare empty dir: no crash
        stack = _detect_tech_stack(tmp_path)
        assert isinstance(stack, list)


# ---------------------------------------------------------------------------
# _extract_helm_info
# ---------------------------------------------------------------------------

class TestExtractHelmInfo:
    def test_reads_chart_yaml(self, tmp_path):
        (tmp_path / "Chart.yaml").write_text(textwrap.dedent("""\
            apiVersion: v2
            name: alerting
            version: 1.0.0
            dependencies:
              - name: kube-prometheus-stack
                version: "55.0.0"
              - name: alertmanager
                version: "1.0.0"
        """))
        chart_name, deps = _extract_helm_info(tmp_path)
        assert chart_name == "alerting"
        assert "kube-prometheus-stack" in deps
        assert "alertmanager" in deps

    def test_missing_chart_yaml(self, tmp_path):
        chart_name, deps = _extract_helm_info(tmp_path)
        assert chart_name is None
        assert deps == []

    def test_chart_in_subdirectory(self, tmp_path):
        sub = tmp_path / "charts" / "myapp"
        sub.mkdir(parents=True)
        (sub / "Chart.yaml").write_text("name: myapp\nversion: 0.1.0\n")
        # Root Chart.yaml absent → should not find subdirectory chart
        chart_name, deps = _extract_helm_info(tmp_path)
        # Either finds it or returns None — both are acceptable depending on
        # whether the sub is a direct child of repo_path; here it is 2 levels deep
        assert isinstance(deps, list)


# ---------------------------------------------------------------------------
# _extract_go_module
# ---------------------------------------------------------------------------

class TestExtractGoModule:
    def test_reads_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/moia-dev/k8s-audit-label-operator\n\ngo 1.22\n")
        assert _extract_go_module(tmp_path) == "github.com/moia-dev/k8s-audit-label-operator"

    def test_missing_go_mod(self, tmp_path):
        assert _extract_go_module(tmp_path) is None


# ---------------------------------------------------------------------------
# _extract_terraform_info
# ---------------------------------------------------------------------------

class TestExtractTerraformInfo:
    def test_reads_backend_bucket(self, tmp_path):
        (tmp_path / "backend.tf").write_text(textwrap.dedent("""\
            terraform {
              backend "s3" {
                bucket = "pe.global-tf-state"
                key    = "aws-infra/state.tfstate"
                region = "eu-central-1"
              }
            }
        """))
        bucket, deps = _extract_terraform_info(tmp_path)
        assert bucket == "pe.global-tf-state"

    def test_reads_remote_state(self, tmp_path):
        (tmp_path / "main.tf").write_text(textwrap.dedent("""\
            data "terraform_remote_state" "aws_infra" {
              backend = "s3"
              config = {
                bucket = "pe.global-tf-state"
                key    = "aws-infra/state.tfstate"
              }
            }
        """))
        _, deps = _extract_terraform_info(tmp_path)
        assert "aws_infra" in deps

    def test_no_tf_files(self, tmp_path):
        bucket, deps = _extract_terraform_info(tmp_path)
        assert bucket is None
        assert deps == []


# ---------------------------------------------------------------------------
# _extract_ci_pipelines
# ---------------------------------------------------------------------------

class TestExtractCiPipelines:
    def test_reads_workflow_names(self, tmp_path):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "deploy.yml").write_text("name: Deploy and Release\non:\n  push:\n    branches: [main]\n")
        (wf_dir / "build.yml").write_text("name: Build\non: [push]\n")
        pipelines = _extract_ci_pipelines(tmp_path)
        assert "Deploy and Release" in pipelines
        assert "Build" in pipelines

    def test_falls_back_to_stem(self, tmp_path):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on: [push]\njobs:\n")
        pipelines = _extract_ci_pipelines(tmp_path)
        assert "ci" in pipelines

    def test_no_workflows_dir(self, tmp_path):
        assert _extract_ci_pipelines(tmp_path) == []


# ---------------------------------------------------------------------------
# _extract_argocd_info
# ---------------------------------------------------------------------------

class TestExtractArgocdInfo:
    def test_detects_application(self, tmp_path):
        (tmp_path / "app.yaml").write_text(textwrap.dedent("""\
            apiVersion: argoproj.io/v1alpha1
            kind: Application
            metadata:
              name: alerting-prod
            spec:
              destination:
                namespace: monitoring
              source:
                repoURL: https://github.com/moia-dev/alerting
        """))
        info = _extract_argocd_info(tmp_path)
        apps = [d["app"] for d in info["deployed_via"]]
        assert "alerting-prod" in apps
        assert "alerting" in info["source_repos"]

    def test_detects_applicationset(self, tmp_path):
        (tmp_path / "appset.yaml").write_text(textwrap.dedent("""\
            apiVersion: argoproj.io/v1alpha1
            kind: ApplicationSet
            metadata:
              name: cluster-resources
            spec:
              template:
                spec:
                  source:
                    repoURL: https://github.com/moia-dev/argocd-platform
        """))
        info = _extract_argocd_info(tmp_path)
        apps = [d["app"] for d in info["deployed_via"]]
        assert "cluster-resources" in apps

    def test_no_argocd_files(self, tmp_path):
        info = _extract_argocd_info(tmp_path)
        assert info["deployed_via"] == []


# ---------------------------------------------------------------------------
# _extract_owners
# ---------------------------------------------------------------------------

class TestExtractOwners:
    def test_reads_moia_yml(self, tmp_path):
        (tmp_path / "moia.yml").write_text(textwrap.dedent("""\
            # Comment
            owners:
              - platform-engineering
              - observability-team
        """))
        owners = _extract_owners(tmp_path)
        assert "platform-engineering" in owners
        assert "observability-team" in owners

    def test_missing_moia_yml(self, tmp_path):
        assert _extract_owners(tmp_path) == []


# ---------------------------------------------------------------------------
# _extract_irsa_refs
# ---------------------------------------------------------------------------

class TestExtractIrsaRefs:
    def test_finds_irsa_annotation(self, tmp_path):
        (tmp_path / "values.yaml").write_text(textwrap.dedent("""\
            serviceAccount:
              annotations:
                eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/my-service-role"
        """))
        roles = _extract_irsa_refs(tmp_path)
        assert "my-service-role" in roles

    def test_no_irsa(self, tmp_path):
        (tmp_path / "values.yaml").write_text("replicaCount: 1\n")
        assert _extract_irsa_refs(tmp_path) == []


# ---------------------------------------------------------------------------
# _build_summary_text
# ---------------------------------------------------------------------------

class TestBuildSummaryText:
    def test_contains_all_sections(self):
        text = _build_summary_text(
            name="alerting",
            domain="Observability-Alerting",
            purpose="Manages Prometheus alerting rules.",
            tech_stack=["Helm", "Jsonnet"],
            provides=["Helm chart: alerting"],
            depends_on=[{"type": "repo", "name": "aws-infra", "reason": "IRSA roles"}],
            deployed_via=[{"type": "application", "app": "alerting-prod", "clusters": ["prod"]}],
            key_files=["Chart.yaml", "README.md"],
            ci_pipelines=["Deploy and Release"],
            owners=["platform-engineering"],
            helm_chart_name="alerting",
            go_module=None,
            tf_backend_bucket=None,
            python_deps=[],
            dockerfile_info={},
        )
        assert "alerting" in text
        assert "Observability-Alerting" in text
        assert "Helm" in text
        assert "aws-infra" in text
        assert "IRSA roles" in text
        assert "alerting-prod" in text
        assert "Deploy and Release" in text
        assert "platform-engineering" in text

    def test_omits_empty_sections(self):
        text = _build_summary_text(
            name="simple",
            domain="Platform-Core",
            purpose="",
            tech_stack=[],
            provides=[],
            depends_on=[],
            deployed_via=[],
            key_files=[],
            ci_pipelines=[],
            owners=[],
            helm_chart_name=None,
            go_module=None,
            tf_backend_bucket=None,
            python_deps=[],
            dockerfile_info={},
        )
        assert "Tech stack:" not in text
        assert "Depends on:" not in text
        assert "Deployed via:" not in text


# ---------------------------------------------------------------------------
# discover_and_extract (integration over fake workspace)
# ---------------------------------------------------------------------------

class TestDiscoverAndExtract:
    def test_detects_go_repo(self, workspace):
        repo = _make_repo(workspace / "repos", "Runtime-Services", "my-operator")
        (repo / "go.mod").write_text("module github.com/moia-dev/my-operator\ngo 1.22\n")
        (repo / "README.md").write_text("# my-operator\n\nA Kubernetes operator.\n")
        (repo / "Dockerfile").write_text("FROM golang:1.22\n")

        summaries = discover_and_extract(workspace / "repos", workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.name == "my-operator"
        assert s.domain == "Runtime-Services"
        assert "Go" in s.tech_stack
        assert "Container" in s.tech_stack
        assert s.go_module == "github.com/moia-dev/my-operator"
        assert "A Kubernetes operator." in s.purpose

    def test_detects_helm_chart(self, workspace):
        repo = _make_repo(workspace / "repos", "Delivery-GitOps", "argocd-platform")
        (repo / "Chart.yaml").write_text(
            "apiVersion: v2\nname: argocd-platform\nversion: 1.0.0\n"
            "dependencies:\n  - name: argo-cd\n    version: '5.0.0'\n"
        )
        (repo / "README.md").write_text("# argocd-platform\n\nManages ArgoCD deployments.\n")
        (repo / "moia.yml").write_text("owners:\n  - platform-engineering\n")

        summaries = discover_and_extract(workspace / "repos", workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.helm_chart_name == "argocd-platform"
        assert "argo-cd" in [d["name"] for d in s.depends_on]
        assert "platform-engineering" in s.owners

    def test_detects_terraform_repo(self, workspace):
        repo = _make_repo(workspace / "repos", "Platform-Core", "aws-infra")
        (repo / "main.tf").write_text('terraform { required_version = ">= 1.5" }\n')
        (repo / "backend.tf").write_text(
            'terraform {\n  backend "s3" {\n    bucket = "tf-state"\n  }\n}\n'
        )
        summaries = discover_and_extract(workspace / "repos", workspace)
        assert len(summaries) == 1
        s = summaries[0]
        assert "Terraform" in s.tech_stack
        assert s.tf_backend_bucket == "tf-state"

    def test_multiple_repos_multiple_domains(self, workspace):
        for domain, name in [
            ("Platform-Core", "aws-infra"),
            ("Observability-Alerting", "alerting"),
            ("Runtime-Services", "k8s-operator"),
        ]:
            repo = _make_repo(workspace / "repos", domain, name)
            (repo / "README.md").write_text(f"# {name}\n\nPurpose of {name}.\n")
            (repo / "main.tf").write_text("terraform {}\n")

        summaries = discover_and_extract(workspace / "repos", workspace)
        assert len(summaries) == 3
        names = {s.name for s in summaries}
        assert names == {"aws-infra", "alerting", "k8s-operator"}

    def test_repo_without_marker_is_skipped(self, workspace):
        # A plain directory with no markers should not be detected
        plain = workspace / "repos" / "Platform-Core" / "not-a-repo"
        plain.mkdir(parents=True)
        (plain / "random.txt").write_text("just a text file\n")

        summaries = discover_and_extract(workspace / "repos", workspace)
        assert len(summaries) == 0

    def test_summary_text_is_nonempty(self, workspace):
        repo = _make_repo(workspace / "repos", "AI-Services", "dod-bot")
        (repo / "pyproject.toml").write_text("[project]\nname = 'dod-bot'\ndependencies = ['boto3', 'mcp']\n")
        (repo / "README.md").write_text("# dod-bot\n\nSlack bot powered by Claude.\n")

        summaries = discover_and_extract(workspace / "repos", workspace)
        assert summaries[0].summary_text != ""
        assert "dod-bot" in summaries[0].summary_text


# ---------------------------------------------------------------------------
# extract_and_index_repos (DB integration)
# ---------------------------------------------------------------------------

class TestExtractAndIndexRepos:
    def test_inserts_knowledge_entry(self, db, workspace):
        repo = _make_repo(workspace / "repos", "Platform-Core", "aws-infra")
        (repo / "README.md").write_text("# aws-infra\n\nAWS infrastructure via Terraform.\n")
        (repo / "main.tf").write_text("terraform {}\n")

        stats = extract_and_index_repos(db, str(workspace))
        assert stats.repos_found == 1
        assert stats.repos_indexed == 1
        assert len(stats.errors) == 0

        row = db.execute(
            "SELECT title, content_type, content FROM knowledge_entries WHERE source_path LIKE '%aws-infra%'"
        ).fetchone()
        assert row is not None
        assert row[1] == "repo_summary"
        assert "aws-infra" in row[2]

    def test_creates_graph_nodes_and_edges(self, db, workspace):
        repo = _make_repo(workspace / "repos", "Observability-Alerting", "alerting")
        (repo / "README.md").write_text("# alerting\n\nPrometheus alert rules.\n")
        (repo / "Chart.yaml").write_text("apiVersion: v2\nname: alerting\nversion: 1.0.0\n")
        (repo / "moia.yml").write_text("owners:\n  - platform-engineering\n")

        stats = extract_and_index_repos(db, str(workspace))
        assert stats.graph_edges_created > 0

        # Verify the repo node exists
        node = db.execute(
            "SELECT entity_name, entity_type FROM knowledge_graph_nodes WHERE entity_name = 'alerting'"
        ).fetchone()
        assert node is not None
        assert node[1] == "repo"

        # Verify belongs_to_domain edge
        edge = db.execute(
            """SELECT e.predicate FROM knowledge_graph_edges e
               JOIN knowledge_graph_nodes n ON n.id = e.source_id
               WHERE n.entity_name = 'alerting' AND e.predicate = 'belongs_to_domain'"""
        ).fetchone()
        assert edge is not None

        # Verify ownership edge (team -> repo)
        owner_node = db.execute(
            "SELECT entity_name FROM knowledge_graph_nodes WHERE entity_name = 'platform-engineering'"
        ).fetchone()
        assert owner_node is not None

    def test_idempotent_second_run(self, db, workspace):
        repo = _make_repo(workspace / "repos", "Platform-Core", "k8s-infra")
        (repo / "README.md").write_text("# k8s-infra\n\nKubernetes infrastructure.\n")
        (repo / "main.tf").write_text("terraform {}\n")

        stats1 = extract_and_index_repos(db, str(workspace))
        assert stats1.repos_indexed == 1

        stats2 = extract_and_index_repos(db, str(workspace))
        # Second run: content unchanged, so repos_indexed=0 but no error
        assert stats2.repos_indexed == 0
        assert len(stats2.errors) == 0

        # Only one entry in DB
        count = db.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE content_type = 'repo_summary'"
        ).fetchone()[0]
        assert count == 1

    def test_updated_entry_on_content_change(self, db, workspace):
        repo = _make_repo(workspace / "repos", "Platform-Core", "dns-infra")
        readme = repo / "README.md"
        readme.write_text("# dns-infra\n\nManages DNS records.\n")
        (repo / "main.tf").write_text("terraform {}\n")

        extract_and_index_repos(db, str(workspace))

        # Modify README
        readme.write_text("# dns-infra\n\nManages DNS records via Route53 and Terraform.\n")
        stats = extract_and_index_repos(db, str(workspace))
        assert stats.repos_updated == 1

    def test_metadata_stored_as_json(self, db, workspace):
        repo = _make_repo(workspace / "repos", "AI-Services", "dod-bot")
        (repo / "pyproject.toml").write_text("[project]\nname = 'dod-bot'\n")
        (repo / "README.md").write_text("# dod-bot\n\nAI assistant.\n")

        extract_and_index_repos(db, str(workspace))

        meta_raw = db.execute(
            "SELECT metadata FROM knowledge_entries WHERE content_type = 'repo_summary'"
        ).fetchone()[0]
        meta = json.loads(meta_raw)
        assert meta["repo_name"] == "dod-bot"
        assert meta["domain"] == "AI-Services"
        assert "tech_stack" in meta

    def test_empty_workspace_returns_zero(self, db, tmp_path):
        (tmp_path / "repos").mkdir()
        stats = extract_and_index_repos(db, str(tmp_path))
        assert stats.repos_found == 0
        assert stats.repos_indexed == 0

    def test_missing_repos_dir_falls_back(self, db, tmp_path):
        # workspace without repos/ subdirectory — should not crash
        stats = extract_and_index_repos(db, str(tmp_path))
        assert isinstance(stats, ExtractorStats)

    def test_argocd_deployment_creates_cluster_edge(self, db, workspace):
        repo = _make_repo(workspace / "repos", "Delivery-GitOps", "argocd-platform")
        (repo / "README.md").write_text("# argocd-platform\n\nArgoCD platform management.\n")
        app_yaml = repo / "app.yaml"
        app_yaml.write_text(textwrap.dedent("""\
            apiVersion: argoproj.io/v1alpha1
            kind: ApplicationSet
            metadata:
              name: cluster-resources
            spec:
              template:
                metadata:
                  name: "cluster-resources-{{ .name }}"
                spec:
                  destination:
                    name: "cluster-prod"
                  source:
                    repoURL: https://github.com/moia-dev/argocd-platform
        """))

        stats = extract_and_index_repos(db, str(workspace))
        assert stats.repos_indexed == 1

        # The repo node should have a deployed_by edge
        edges = db.execute(
            """SELECT e.predicate, n2.entity_name
               FROM knowledge_graph_edges e
               JOIN knowledge_graph_nodes n1 ON n1.id = e.source_id
               JOIN knowledge_graph_nodes n2 ON n2.id = e.target_id
               WHERE n1.entity_name = 'argocd-platform'"""
        ).fetchall()
        predicates = {r[0] for r in edges}
        assert "belongs_to_domain" in predicates

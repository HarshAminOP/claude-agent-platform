"""Cross-repo dependency discovery and resolution for the CAP knowledge graph.

Builds on repo_resolver (cloning) and repo_extractor (basic extraction) to
produce a complete, transitive dependency graph covering:
  - Terraform remote_state and module sources
  - Helm Chart.yaml dependencies
  - ArgoCD Application/ApplicationSet repoURL references
  - Go internal import paths (github.com/moia-oss/*, github.com/moia-dev/*)
  - Python org-internal package dependencies
  - GitHub Actions reusable workflow references

Usage::

    from cap.lib.dependency_resolver import DependencyResolver, DependencyResolverConfig

    resolver = DependencyResolver(db, DependencyResolverConfig())
    sources  = resolver.discover_all_sources(["/path/to/workspace"])
    dep_graph = resolver.resolve_dependencies(sources)
    stats    = resolver.build_full_graph(dep_graph, workspace="/path/to/workspace")
"""

import fnmatch
import json
import logging
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cap.lib.config import PlatformConfig, load_config
from cap.lib.graph import add_edge

logger = logging.getLogger("cap.dependency_resolver")

# ---------------------------------------------------------------------------
# Safety guard: reject names that could cause path traversal or injection
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._-]{0,99}[a-zA-Z0-9])?$')

_READ_SIZE_CAP = 64 * 1024  # 64 KB — consistent with repo_extractor

# Directories never scanned for repo roots or source files
_SKIP_DIRS = frozenset({
    ".git", ".hg", "node_modules", "vendor", ".vendor",
    ".terraform", "__pycache__", ".venv", "venv",
    "dist", "build", "_build",
})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class DependencyResolverConfig:
    """Tuning knobs for the dependency resolution run."""

    max_depth: int = 3
    """Maximum levels of transitive dependencies to resolve."""

    auto_clone: bool = True
    """Clone missing repos automatically via repo_resolver."""

    scan_terraform: bool = True
    scan_helm: bool = True
    scan_argocd: bool = True
    scan_go_imports: bool = True
    scan_python_imports: bool = True
    scan_github_actions: bool = True

    max_repos_per_run: int = 50
    """Safety limit — stop after scanning this many repos."""

    timeout_per_repo_seconds: int = 30
    """Wall-clock budget per repo during resolution (not enforced via SIGALRM;
    used to gate subprocess calls)."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RepoSource:
    """A git repo discovered on disk."""

    name: str
    path: str
    """Absolute filesystem path."""

    domain: str
    """Parent directory name (e.g. 'Observability-Alerting')."""

    git_remote: Optional[str] = None
    last_commit_sha: Optional[str] = None


@dataclass
class Dependency:
    """A single dependency edge discovered by a scanner."""

    source_repo: str
    target: str
    """Repo name, module path, chart name, workflow ref, etc."""

    dep_type: str
    """One of: terraform_remote_state, terraform_module, helm_dep,
    argocd_repo, go_import, python_import, github_action."""

    reference: str
    """The raw reference string as found in the source file."""

    resolved: bool = False
    resolved_path: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class DependencyGraph:
    """Full output of resolve_dependencies()."""

    sources: list[RepoSource]
    dependencies: list[Dependency]
    unresolved: list[Dependency]
    resolution_stats: dict = field(default_factory=dict)

    @property
    def resolved_count(self) -> int:
        """Number of dependencies that were successfully resolved to a local path."""
        return sum(1 for d in self.dependencies if d.resolved)


@dataclass
class GraphStats:
    """Counters returned by build_full_graph()."""

    nodes_created: int = 0
    edges_created: int = 0
    nodes_updated: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DependencyResolver
# ---------------------------------------------------------------------------

class DependencyResolver:
    """Discover and resolve cross-repo dependencies into the knowledge graph.

    Args:
        db:              Active SQLite connection to knowledge.db.
        config:          Resolver configuration; defaults are used when None.
        platform_config: Platform-wide config (GitHub org, clone paths, etc.).
                         Loaded from disk when None.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        config: Optional[DependencyResolverConfig] = None,
        platform_config: Optional[PlatformConfig] = None,
    ) -> None:
        self._db = db
        self._config = config or DependencyResolverConfig()
        self._platform_config = platform_config or load_config()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_all_sources(self, workspace_roots: list[str]) -> list[RepoSource]:
        """Walk workspace directories and return every git repo found on disk.

        Args:
            workspace_roots: List of absolute paths to scan.

        Returns:
            Deduplicated list of RepoSource objects, one per git repo.
        """
        sources: list[RepoSource] = []
        seen_paths: set[str] = set()

        for root_str in workspace_roots:
            root = Path(root_str)
            if not root.is_dir():
                logger.debug("discover_all_sources: skipping non-existent root %s", root)
                continue

            for domain_dir in sorted(root.iterdir()):
                if not domain_dir.is_dir():
                    continue
                if domain_dir.name.startswith(".") or domain_dir.name in _SKIP_DIRS:
                    continue

                for candidate in sorted(domain_dir.iterdir()):
                    if not candidate.is_dir():
                        continue
                    if candidate.name.startswith(".") or candidate.name in _SKIP_DIRS:
                        continue

                    abs_path = str(candidate.resolve())
                    if abs_path in seen_paths:
                        continue

                    if not (candidate / ".git").exists():
                        continue

                    seen_paths.add(abs_path)
                    git_remote = self._get_git_remote(candidate)
                    last_sha = self._get_last_commit_sha(candidate)

                    sources.append(RepoSource(
                        name=candidate.name,
                        path=abs_path,
                        domain=domain_dir.name,
                        git_remote=git_remote,
                        last_commit_sha=last_sha,
                    ))

        logger.info("discover_all_sources: found %d repos across %d roots", len(sources), len(workspace_roots))
        return sources

    def resolve_dependencies(self, sources: list[RepoSource]) -> DependencyGraph:
        """Scan every source repo for dependency references and attempt resolution.

        Runs configured scanners (Terraform, Helm, ArgoCD, Go, Python,
        GitHub Actions) on each repo up to ``max_repos_per_run``.  For every
        unresolved dependency, attempts to find or clone the target repo.

        Args:
            sources: Repos to scan, as returned by ``discover_all_sources()``.

        Returns:
            DependencyGraph with all discovered and (where possible) resolved
            dependencies.
        """
        all_deps: list[Dependency] = []
        scanned = 0

        for source in sources:
            if scanned >= self._config.max_repos_per_run:
                logger.warning(
                    "resolve_dependencies: hit max_repos_per_run=%d, stopping",
                    self._config.max_repos_per_run,
                )
                break

            repo_path = Path(source.path)
            if not repo_path.is_dir():
                logger.debug("Skipping missing repo path: %s", source.path)
                continue

            deps = self._scan_repo(repo_path, source.name)
            all_deps.extend(deps)
            scanned += 1
            logger.debug("Scanned %s: %d dependencies found", source.name, len(deps))

        # Resolution pass
        resolved_deps: list[Dependency] = []
        unresolved_deps: list[Dependency] = []

        clone_base = self._clone_base()

        for dep in all_deps:
            resolved = self._resolve_dependency(dep, clone_base)
            resolved_deps.append(resolved)
            if not resolved.resolved:
                unresolved_deps.append(resolved)

        stats = {
            "repos_scanned": scanned,
            "total_deps": len(all_deps),
            "resolved": sum(1 for d in resolved_deps if d.resolved),
            "unresolved": len(unresolved_deps),
        }
        logger.info(
            "resolve_dependencies: scanned=%d total=%d resolved=%d unresolved=%d",
            scanned, stats["total_deps"], stats["resolved"], stats["unresolved"],
        )

        return DependencyGraph(
            sources=sources[:scanned],
            dependencies=resolved_deps,
            unresolved=unresolved_deps,
            resolution_stats=stats,
        )

    def build_full_graph(self, dep_graph: DependencyGraph, workspace: str) -> GraphStats:
        """Write all discovered dependencies into the knowledge graph.

        Creates edges for every dependency in *dep_graph*:
          - Repo → depends_on → Repo
          - Repo → uses_module → TerraformModule
          - Repo → uses_chart → HelmChart
          - Repo → uses_workflow → GitHubAction

        Also emits ``provisions`` edges for AWS resources found inside .tf files
        that belong to resolved Terraform module repos.

        Args:
            dep_graph: Output of ``resolve_dependencies()``.
            workspace: Workspace identifier used to scope graph edges.

        Returns:
            GraphStats with counts of created/updated nodes and edges.
        """
        stats = GraphStats()

        _PREDICATE_MAP: dict[str, tuple[str, str, str]] = {
            # dep_type: (source_entity_type, predicate, target_entity_type)
            "terraform_remote_state": ("repo", "depends_on",    "repo"),
            "terraform_module":       ("repo", "uses_module",   "terraform_module"),
            "helm_dep":               ("repo", "uses_chart",    "helm_chart"),
            "argocd_repo":            ("repo", "depends_on",    "repo"),
            "go_import":              ("repo", "depends_on",    "repo"),
            "python_import":          ("repo", "depends_on",    "repo"),
            "github_action":          ("repo", "uses_workflow", "github_action"),
        }

        for dep in dep_graph.dependencies:
            mapping = _PREDICATE_MAP.get(dep.dep_type)
            if mapping is None:
                logger.debug("Unknown dep_type %r for %s → %s", dep.dep_type, dep.source_repo, dep.target)
                continue

            src_type, predicate, tgt_type = mapping

            meta: dict = {"dep_type": dep.dep_type, "reference": dep.reference[:200]}
            if dep.resolved and dep.resolved_path:
                meta["resolved_path"] = dep.resolved_path
            if dep.metadata:
                meta.update({k: v for k, v in dep.metadata.items() if k not in meta})

            try:
                add_edge(
                    self._db,
                    source_name=dep.source_repo,
                    source_type=src_type,
                    target_name=dep.target,
                    target_type=tgt_type,
                    predicate=predicate,
                    workspace=workspace,
                    metadata=meta,
                )
                stats.edges_created += 1
            except Exception as exc:
                err = f"Edge {dep.source_repo} -[{predicate}]-> {dep.target}: {exc}"
                stats.errors.append(err)
                logger.debug(err)

        # Emit provisions edges for AWS resources in resolved Terraform modules
        for dep in dep_graph.dependencies:
            if dep.dep_type != "terraform_module" or not dep.resolved or not dep.resolved_path:
                continue
            aws_resources = self._extract_aws_resources(Path(dep.resolved_path))
            for resource in aws_resources:
                try:
                    add_edge(
                        self._db,
                        source_name=dep.target,
                        source_type="terraform_module",
                        target_name=resource,
                        target_type="aws_resource",
                        predicate="provisions",
                        workspace=workspace,
                        metadata={"source_repo": dep.source_repo},
                    )
                    stats.edges_created += 1
                except Exception as exc:
                    stats.errors.append(f"provisions edge {dep.target} → {resource}: {exc}")

        try:
            self._db.commit()
        except Exception as exc:
            stats.errors.append(f"commit failed: {exc}")
            logger.error("build_full_graph: commit failed: %s", exc)

        logger.info(
            "build_full_graph: edges_created=%d errors=%d",
            stats.edges_created, len(stats.errors),
        )
        return stats

    def get_dependency_tree(self, repo_name: str, max_depth: int = 3) -> dict:
        """Return the full dependency tree for a repo as a nested dict.

        Queries the knowledge graph for ``depends_on`` / ``uses_module`` /
        ``uses_chart`` / ``uses_workflow`` edges up to *max_depth* hops.

        Args:
            repo_name: Name of the root repo.
            max_depth: Maximum levels of transitive dependencies to include.

        Returns:
            Nested dict ``{"name": repo_name, "deps": [...]}`` where each entry
            in ``deps`` has the same shape.  Cycles are detected and represented
            as ``{"name": "...", "deps": [], "cycle": True}``.
        """
        return self._build_tree(repo_name, max_depth=max_depth, visited=set())

    def find_dependents(self, repo_name: str) -> list[str]:
        """Return names of all repos that directly depend on *repo_name*.

        Performs a reverse-lookup in the knowledge graph for any edge whose
        target is *repo_name* and whose predicate indicates a dependency
        (``depends_on``, ``uses_module``, ``uses_chart``).

        Args:
            repo_name: Name of the target repo.

        Returns:
            Sorted list of source repo names.
        """
        predicates = ("depends_on", "uses_module", "uses_chart", "uses_workflow")
        placeholders = ",".join("?" * len(predicates))

        rows = self._db.execute(
            f"""
            SELECT DISTINCT src.entity_name
            FROM   knowledge_graph_edges e
            JOIN   knowledge_graph_nodes src ON src.id = e.source_id
            JOIN   knowledge_graph_nodes tgt ON tgt.id = e.target_id
            WHERE  tgt.entity_name = ?
              AND  e.predicate IN ({placeholders})
            """,
            [repo_name] + list(predicates),
        ).fetchall()

        return sorted(r[0] for r in rows)

    # ------------------------------------------------------------------
    # Private: per-repo orchestration
    # ------------------------------------------------------------------

    def _scan_repo(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Run all enabled scanners on a single repo and return combined deps."""
        deps: list[Dependency] = []

        if self._config.scan_terraform:
            try:
                deps.extend(self._scan_terraform_deps(repo_path, repo_name))
            except Exception as exc:
                logger.debug("terraform scan failed for %s: %s", repo_name, exc)

        if self._config.scan_helm:
            try:
                deps.extend(self._scan_helm_deps(repo_path, repo_name))
            except Exception as exc:
                logger.debug("helm scan failed for %s: %s", repo_name, exc)

        if self._config.scan_argocd:
            try:
                deps.extend(self._scan_argocd_deps(repo_path, repo_name))
            except Exception as exc:
                logger.debug("argocd scan failed for %s: %s", repo_name, exc)

        if self._config.scan_go_imports:
            try:
                deps.extend(self._scan_go_deps(repo_path, repo_name))
            except Exception as exc:
                logger.debug("go scan failed for %s: %s", repo_name, exc)

        if self._config.scan_python_imports:
            try:
                deps.extend(self._scan_python_deps(repo_path, repo_name))
            except Exception as exc:
                logger.debug("python scan failed for %s: %s", repo_name, exc)

        if self._config.scan_github_actions:
            try:
                deps.extend(self._scan_github_actions(repo_path, repo_name))
            except Exception as exc:
                logger.debug("github actions scan failed for %s: %s", repo_name, exc)

        return deps

    # ------------------------------------------------------------------
    # Private: scanners
    # ------------------------------------------------------------------

    def _scan_terraform_deps(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan .tf files for remote_state references and module sources.

        Extracts:
        - ``data "terraform_remote_state" "..."`` blocks → dep_type=terraform_remote_state
        - ``module "..." { source = "..." }`` blocks → dep_type=terraform_module

        Returns:
            List of Dependency objects with raw references.
        """
        deps: list[Dependency] = []
        seen_refs: set[str] = set()

        tf_files = _find_files(repo_path, "*.tf", max_depth=4)
        for tf_path in tf_files[:40]:
            content = _read_file(tf_path)
            if not content:
                continue

            logger.debug("terraform scan: %s", tf_path)

            # terraform_remote_state — capture both the alias and the key
            for match in re.finditer(
                r'data\s+"terraform_remote_state"\s+"([^"]+)"\s*\{([^}]*)\}',
                content,
                re.DOTALL,
            ):
                alias = match.group(1)
                block = match.group(2)
                # Try to extract key = "path/..." to get a concrete repo name
                key_match = re.search(r'key\s*=\s*"([^"]+)"', block)
                if key_match:
                    target = key_match.group(1).split("/")[0].rstrip("/")
                else:
                    target = alias

                ref_key = f"remote_state:{target}"
                if ref_key not in seen_refs and target:
                    seen_refs.add(ref_key)
                    deps.append(Dependency(
                        source_repo=repo_name,
                        target=target,
                        dep_type="terraform_remote_state",
                        reference=match.group(0)[:200],
                        metadata={"alias": alias},
                    ))

            # module sources
            for match in re.finditer(
                r'module\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
                content,
                re.DOTALL,
            ):
                block = match.group(2)
                src_match = re.search(r'source\s*=\s*"([^"]+)"', block)
                if not src_match:
                    continue
                source_val = src_match.group(1).strip()
                target = self._terraform_module_target(source_val)
                if not target:
                    continue
                ref_key = f"module:{source_val}"
                if ref_key not in seen_refs:
                    seen_refs.add(ref_key)
                    deps.append(Dependency(
                        source_repo=repo_name,
                        target=target,
                        dep_type="terraform_module",
                        reference=source_val,
                        metadata={"module_name": match.group(1), "source": source_val},
                    ))

        return deps

    def _scan_helm_deps(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan Chart.yaml files for Helm chart dependencies.

        Walks all Chart.yaml files up to 4 levels deep and extracts the
        ``dependencies`` list entries.

        Returns:
            List of Dependency objects with dep_type=helm_dep.
        """
        deps: list[Dependency] = []
        seen_refs: set[str] = set()

        chart_files = _find_files(repo_path, "Chart.yaml", max_depth=4)
        for chart_path in chart_files:
            content = _read_file(chart_path)
            if not content:
                continue

            logger.debug("helm scan: %s", chart_path)

            # Parse dependencies section: list entries with 'name:' and optionally 'repository:'
            in_deps = False
            current_dep: dict = {}

            for line in content.splitlines():
                stripped = line.strip()
                if re.match(r'^dependencies\s*:', stripped):
                    in_deps = True
                    continue
                if in_deps:
                    # Section end when a new top-level key appears
                    if stripped and not stripped.startswith("-") and not stripped.startswith("#") and ":" in stripped:
                        indent = len(line) - len(line.lstrip())
                        if indent == 0:
                            in_deps = False
                            if current_dep.get("name"):
                                _flush_helm_dep(current_dep, repo_name, seen_refs, deps)
                            current_dep = {}
                            continue

                    name_match = re.match(r'[-\s]*name\s*:\s*(.+)', stripped)
                    if name_match:
                        if current_dep.get("name"):
                            _flush_helm_dep(current_dep, repo_name, seen_refs, deps)
                            current_dep = {}
                        current_dep["name"] = name_match.group(1).strip().strip("'\"")

                    repo_match = re.match(r'[-\s]*repository\s*:\s*(.+)', stripped)
                    if repo_match:
                        current_dep["repository"] = repo_match.group(1).strip().strip("'\"")

                    ver_match = re.match(r'[-\s]*version\s*:\s*(.+)', stripped)
                    if ver_match:
                        current_dep["version"] = ver_match.group(1).strip().strip("'\"")

            # Flush last dep
            if current_dep.get("name"):
                _flush_helm_dep(current_dep, repo_name, seen_refs, deps)

        return deps

    def _scan_argocd_deps(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan YAML files for ArgoCD Application/ApplicationSet repoURL references.

        Returns:
            List of Dependency objects with dep_type=argocd_repo.
        """
        deps: list[Dependency] = []
        seen_refs: set[str] = set()

        yaml_files = (
            _find_files(repo_path, "*.yaml", max_depth=4) +
            _find_files(repo_path, "*.yml",  max_depth=4)
        )

        for yf in yaml_files[:60]:
            content = _read_file(yf)
            if not content:
                continue
            if "argoproj.io" not in content and "argocd" not in content.lower():
                continue
            if not re.search(r'^kind:\s*Application(?:Set)?', content, re.MULTILINE):
                continue

            logger.debug("argocd scan: %s", yf)

            for match in re.finditer(r'repoURL\s*:\s*[\'"]?([^\s\'"]+)', content):
                url = match.group(1).strip().rstrip("/")
                repo_target = url.split("/")[-1].removesuffix(".git")
                ref_key = f"argocd:{url}"
                if ref_key not in seen_refs and repo_target and repo_target != repo_name:
                    seen_refs.add(ref_key)
                    deps.append(Dependency(
                        source_repo=repo_name,
                        target=repo_target,
                        dep_type="argocd_repo",
                        reference=url,
                        metadata={"full_url": url},
                    ))

        return deps

    def _scan_go_deps(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan go.mod for org-internal module dependencies.

        Matches import paths starting with ``github.com/moia-oss/`` or
        ``github.com/moia-dev/``.

        Returns:
            List of Dependency objects with dep_type=go_import.
        """
        deps: list[Dependency] = []
        go_mod = repo_path / "go.mod"
        if not go_mod.exists():
            return deps

        content = _read_file(go_mod)
        if not content:
            return deps

        logger.debug("go scan: %s", go_mod)

        gh_config = self._platform_config.github
        org = gh_config.org or ""
        org_aliases: list[str] = list(gh_config.org_aliases) if hasattr(gh_config, "org_aliases") else []

        # Build pattern to match org-internal imports.
        # Patterns are constructed from the configured org and any aliases — no
        # hardcoded org names so the resolver is portable across organisations.
        org_patterns: list = []
        all_orgs = ([org] if org else []) + [a for a in org_aliases if a]
        for o in all_orgs:
            org_patterns.append(re.compile(rf'github\.com/{re.escape(o)}/([a-zA-Z0-9._-]+)'))

        seen_refs: set[str] = set()
        in_require = False

        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "require (":
                in_require = True
                continue
            if in_require and stripped == ")":
                in_require = False
                continue

            if not in_require and not stripped.startswith("require "):
                continue

            for pattern in org_patterns:
                for m in pattern.finditer(stripped):
                    target = m.group(1).removesuffix(".git")
                    full_ref = m.group(0)
                    ref_key = f"go:{full_ref}"
                    if ref_key not in seen_refs and target != repo_name:
                        seen_refs.add(ref_key)
                        deps.append(Dependency(
                            source_repo=repo_name,
                            target=target,
                            dep_type="go_import",
                            reference=full_ref,
                            metadata={"import_path": full_ref},
                        ))

        return deps

    def _scan_python_deps(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan pyproject.toml and requirements.txt for org-internal Python packages.

        An org-internal package is one that starts with a known org prefix
        (``moia-``, ``cap-``) or matches packages listed in the GitHub org's
        namespace.

        Returns:
            List of Dependency objects with dep_type=python_import.
        """
        deps: list[Dependency] = []
        seen_refs: set[str] = set()

        gh_config = self._platform_config.github
        org = gh_config.org or ""
        org_aliases: list[str] = list(gh_config.org_aliases) if hasattr(gh_config, "org_aliases") else []

        # Heuristic prefixes for org-internal packages.
        # Built from the configured org plus any aliases — no hardcoded org names.
        org_prefixes: list[str] = []
        all_orgs_py = ([org] if org else []) + [a for a in org_aliases if a]
        for o in all_orgs_py:
            normalized = o.lower().replace("-", "_")
            # Match packages whose name starts with the org name or its normalised form
            # e.g. org="my-org" → prefixes "my-org-" and "my_org_"
            org_prefixes.append(f"{o.lower()}-")
            org_prefixes.append(f"{normalized}_")

        def _is_internal(pkg: str) -> bool:
            lp = pkg.lower()
            return any(lp.startswith(p) for p in org_prefixes)

        # pyproject.toml
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            content = _read_file(pyproject)
            if content:
                logger.debug("python scan: %s", pyproject)
                in_deps = False
                for line in content.splitlines():
                    stripped = line.strip()
                    if re.match(r'^dependencies\s*=', stripped):
                        in_deps = True
                        continue
                    if in_deps:
                        if stripped.startswith("[") and not stripped.startswith('["'):
                            in_deps = False
                            continue
                        pkg_match = re.search(r'"([a-zA-Z0-9_-]+)', stripped)
                        if pkg_match:
                            pkg = pkg_match.group(1)
                            if _is_internal(pkg) and pkg not in seen_refs:
                                seen_refs.add(pkg)
                                target = pkg.replace("_", "-")
                                deps.append(Dependency(
                                    source_repo=repo_name,
                                    target=target,
                                    dep_type="python_import",
                                    reference=pkg,
                                    metadata={"package": pkg},
                                ))

        # requirements.txt
        req_file = repo_path / "requirements.txt"
        if req_file.exists():
            content = _read_file(req_file)
            if content:
                logger.debug("python scan: %s", req_file)
                for line in content.splitlines()[:50]:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue
                    pkg = re.split(r"[>=<!~\[]", line)[0].strip().lower()
                    if pkg and _is_internal(pkg) and pkg not in seen_refs:
                        seen_refs.add(pkg)
                        target = pkg.replace("_", "-")
                        deps.append(Dependency(
                            source_repo=repo_name,
                            target=target,
                            dep_type="python_import",
                            reference=pkg,
                            metadata={"package": pkg},
                        ))

        return deps

    def _scan_github_actions(self, repo_path: Path, repo_name: str) -> list[Dependency]:
        """Scan .github/workflows/*.yml for reusable workflow ``uses:`` references.

        Only records references that point to another org repo
        (``org/repo/.github/workflows/...``), not public action marketplace refs
        or local file references (``./...``).

        Returns:
            List of Dependency objects with dep_type=github_action.
        """
        deps: list[Dependency] = []
        seen_refs: set[str] = set()

        workflows_dir = repo_path / ".github" / "workflows"
        if not workflows_dir.is_dir():
            return deps

        gh_config = self._platform_config.github
        org = gh_config.org or ""
        org_aliases: list[str] = list(gh_config.org_aliases) if hasattr(gh_config, "org_aliases") else []

        for wf_file in sorted(workflows_dir.glob("*.yml")):
            content = _read_file(wf_file)
            if not content:
                continue

            logger.debug("github actions scan: %s", wf_file)

            for match in re.finditer(r'uses\s*:\s*["\']?([^\s"\'#]+)', content):
                ref = match.group(1).strip()

                # Skip local file references
                if ref.startswith("./") or ref.startswith("../"):
                    continue

                # Skip Docker container actions
                if ref.startswith("docker://"):
                    continue

                # Match org/repo/... pattern
                parts = ref.split("/")
                if len(parts) < 2:
                    continue

                ref_org = parts[0]
                ref_repo = parts[1].split("@")[0] if "@" in parts[1] else parts[1]

                # Only track refs belonging to the configured org or any alias
                _all_orgs_ref = ([org] if org else []) + (org_aliases if org_aliases else [])
                is_org_internal = bool(_all_orgs_ref) and ref_org in _all_orgs_ref
                if not is_org_internal:
                    continue

                if ref not in seen_refs and ref_repo != repo_name:
                    seen_refs.add(ref)
                    deps.append(Dependency(
                        source_repo=repo_name,
                        target=ref_repo,
                        dep_type="github_action",
                        reference=ref,
                        metadata={"workflow_file": wf_file.name, "full_ref": ref},
                    ))

        return deps

    # ------------------------------------------------------------------
    # Private: resolution
    # ------------------------------------------------------------------

    def _resolve_dependency(self, dep: Dependency, clone_base: Optional[Path]) -> Dependency:
        """Attempt to resolve a single dependency to a local path.

        Strategy by dep_type:
        - terraform_remote_state / argocd_repo / go_import / python_import:
            treat target as a repo name; find or clone via repo_resolver.
        - terraform_module (git source):
            extract repo name from URL; find or clone.
        - helm_dep:
            check if a local repo with that chart name exists.
        - github_action:
            treat target as a repo name; find or clone.

        Args:
            dep:        Dependency to resolve.
            clone_base: Base path for local repo lookup.

        Returns:
            A new Dependency with resolved=True/False and resolved_path set.
        """
        if clone_base is None:
            return dep

        target_name = _safe_target_name(dep.target)
        if not target_name:
            return dep

        local_path: Optional[Path] = None

        if dep.dep_type in (
            "terraform_remote_state", "argocd_repo",
            "go_import", "python_import", "github_action",
        ):
            local_path = _find_local_repo(target_name, clone_base)
            if local_path is None and self._config.auto_clone:
                local_path = self._try_clone(target_name)

        elif dep.dep_type == "terraform_module":
            # For git:// or github.com sources, target is already the repo name
            local_path = _find_local_repo(target_name, clone_base)
            if local_path is None and self._config.auto_clone:
                local_path = self._try_clone(target_name)

        elif dep.dep_type == "helm_dep":
            # Try to find a repo whose name matches the chart name
            local_path = _find_local_repo(target_name, clone_base)

        if local_path is not None:
            return Dependency(
                source_repo=dep.source_repo,
                target=dep.target,
                dep_type=dep.dep_type,
                reference=dep.reference,
                resolved=True,
                resolved_path=str(local_path),
                metadata=dep.metadata,
            )

        return dep

    def _try_clone(self, repo_name: str) -> Optional[Path]:
        """Attempt to clone a missing repo via repo_resolver.

        Args:
            repo_name: Name of the repo to clone.

        Returns:
            Path to the cloned repo, or None on failure.
        """
        try:
            from cap.lib.repo_resolver import resolve_repo
            result = resolve_repo(repo_name, db=self._db, config=self._platform_config.github)
            if result.get("path"):
                return Path(result["path"])
        except Exception as exc:
            logger.debug("Clone attempt failed for %s: %s", repo_name, exc)
        return None

    # ------------------------------------------------------------------
    # Private: graph tree helpers
    # ------------------------------------------------------------------

    def _build_tree(self, repo_name: str, max_depth: int, visited: set[str]) -> dict:
        """Recursive (depth-limited, cycle-safe) dependency tree builder."""
        node: dict = {"name": repo_name, "deps": []}

        if repo_name in visited:
            node["cycle"] = True
            return node

        if max_depth <= 0:
            return node

        visited = visited | {repo_name}

        rows = self._db.execute(
            """
            SELECT DISTINCT tgt.entity_name, e.predicate
            FROM   knowledge_graph_edges e
            JOIN   knowledge_graph_nodes src ON src.id = e.source_id
            JOIN   knowledge_graph_nodes tgt ON tgt.id = e.target_id
            WHERE  src.entity_name = ?
              AND  e.predicate IN ('depends_on','uses_module','uses_chart','uses_workflow')
            """,
            (repo_name,),
        ).fetchall()

        for (target_name, predicate) in rows:
            child = self._build_tree(target_name, max_depth - 1, visited)
            child["predicate"] = predicate
            node["deps"].append(child)

        return node

    # ------------------------------------------------------------------
    # Private: utilities
    # ------------------------------------------------------------------

    def _clone_base(self) -> Optional[Path]:
        """Return the base path for local repo lookup."""
        gh = self._platform_config.github
        if gh.clone_base_path:
            return Path(gh.clone_base_path)
        return None

    @staticmethod
    def _get_git_remote(repo_path: Path) -> Optional[str]:
        """Read the remote URL from git config without network I/O."""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    @staticmethod
    def _get_last_commit_sha(repo_path: Path) -> Optional[str]:
        """Read HEAD SHA from git without network I/O."""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    @staticmethod
    def _terraform_module_target(source: str) -> Optional[str]:
        """Extract a repo name from a Terraform module source string.

        Handles:
        - git::https://github.com/org/repo.git//subdir?ref=v1.0
        - github.com/org/repo//subdir
        - git@github.com:org/repo.git
        - Terraform registry: namespace/module/provider
        - Local paths: ./modules/submod (skipped — not cross-repo)

        Returns:
            Repo name string, or None if not extractable / local.
        """
        # Skip local references
        if source.startswith("./") or source.startswith("../"):
            return None

        # git::https://github.com/org/repo.git or git::ssh://...
        git_https = re.search(r'github\.com[:/]([^/]+)/([a-zA-Z0-9._-]+?)(?:\.git)?(?://|$|\?)', source)
        if git_https:
            return git_https.group(2)

        # SSH: git@github.com:org/repo.git
        ssh_match = re.match(r'git@github\.com:([^/]+)/([a-zA-Z0-9._-]+?)(?:\.git)?$', source)
        if ssh_match:
            return ssh_match.group(2)

        # Terraform registry: namespace/module/provider → use module name
        reg_match = re.match(r'^([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)$', source)
        if reg_match:
            return f"{reg_match.group(1)}-{reg_match.group(2)}"

        return None

    @staticmethod
    def _extract_aws_resources(module_path: Path) -> list[str]:
        """Extract AWS resource types provisioned inside a Terraform module path.

        Scans .tf files for ``resource "aws_*" "..."`` declarations and returns
        the unique resource type names (e.g. ``aws_s3_bucket``).

        Args:
            module_path: Path to the Terraform module directory.

        Returns:
            List of unique AWS resource type strings.
        """
        resources: set[str] = set()
        tf_files = _find_files(module_path, "*.tf", max_depth=3)

        for tf_path in tf_files[:20]:
            content = _read_file(tf_path)
            if not content:
                continue
            for m in re.finditer(r'resource\s+"(aws_[a-zA-Z0-9_]+)"\s+"[^"]+"', content):
                resources.add(m.group(1))

        return sorted(resources)


# ---------------------------------------------------------------------------
# Module-level helpers (no class state; reusable by tests and other modules)
# ---------------------------------------------------------------------------

def _read_file(path: Path, max_bytes: int = _READ_SIZE_CAP) -> Optional[str]:
    """Read a text file safely; return None on error or when oversized."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return None


def _find_files(root: Path, pattern: str, max_depth: int = 3) -> list[Path]:
    """Recursively find files matching *pattern* up to *max_depth* levels deep.

    Skips directories listed in ``_SKIP_DIRS`` and hidden directories.
    """
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) > max_depth:
            dirnames.clear()
            continue
        for fname in filenames:
            if fnmatch.fnmatch(fname, pattern):
                results.append(Path(dirpath) / fname)
    return results


def _find_local_repo(repo_name: str, clone_base: Path) -> Optional[Path]:
    """Search for a git repo by name under *clone_base*.

    Checks:
    1. ``clone_base/repo_name`` (direct)
    2. ``clone_base/*/repo_name`` (one domain level deep)

    Args:
        repo_name:  Name of the repo to find.
        clone_base: Root directory to search within.

    Returns:
        Absolute path to the repo directory, or None if not found.
    """
    if not clone_base.exists():
        return None

    direct = clone_base / repo_name
    if direct.is_dir() and (direct / ".git").exists():
        return direct

    for domain_dir in clone_base.iterdir():
        if not domain_dir.is_dir() or domain_dir.is_symlink() or domain_dir.name.startswith("."):
            continue
        candidate = domain_dir / repo_name
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate

    return None


def _safe_target_name(target: str) -> Optional[str]:
    """Return *target* if it is a safe name, else None.

    Rejects empty strings, path traversal attempts, and names that don't match
    the allowed character set.
    """
    if not target:
        return None
    if ".." in target or "/" in target or "\\" in target:
        return None
    if _SAFE_NAME.match(target):
        return target
    return None


def _flush_helm_dep(
    dep_info: dict,
    repo_name: str,
    seen_refs: set[str],
    deps: list[Dependency],
) -> None:
    """Emit a Dependency for a parsed Helm dependency dict if not already seen."""
    name = dep_info.get("name", "")
    if not name:
        return
    ref_key = f"helm:{name}"
    if ref_key in seen_refs:
        return
    seen_refs.add(ref_key)
    deps.append(Dependency(
        source_repo=repo_name,
        target=name,
        dep_type="helm_dep",
        reference=name,
        metadata={
            "repository": dep_info.get("repository", ""),
            "version": dep_info.get("version", ""),
        },
    ))

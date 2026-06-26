"""Repo-level knowledge extraction for the CAP knowledge base.

Instead of indexing 6,000+ individual files, this module walks workspace/repos/
and produces ONE structured knowledge entry per git repo — a prose summary that
combines README purpose, tech stack, dependencies, deployment targets, and
key files into a single FTS5-indexable document.

Usage::

    from cap.lib.repo_extractor import extract_and_index_repos

    stats = extract_and_index_repos(db, "/path/to/workspace")

Output: ``RepoSummary`` dataclasses, one per detected repo, plus graph nodes and
edges stored into knowledge_entries (content_type='repo_summary').
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cap.lib.graph import add_edge

logger = logging.getLogger("cap.repo_extractor")

# ── Repo detection heuristics ─────────────────────────────────────────────────

# A directory is treated as a repo root if it contains any of these markers.
REPO_MARKERS = frozenset({
    ".git",
    "README.md", "README.rst", "README.txt", "readme.md",
    "go.mod",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json",
    "Chart.yaml",
    "main.tf", "versions.tf",
    "Dockerfile",
    "moia.yml",
})

# Tech stack detection: marker file (glob-like) -> stack name
TECH_STACK_MARKERS = [
    ("*.tf",          "Terraform"),
    ("main.tf",       "Terraform"),
    ("versions.tf",   "Terraform"),
    ("Chart.yaml",    "Helm"),
    ("go.mod",        "Go"),
    ("pyproject.toml","Python"),
    ("setup.py",      "Python"),
    ("requirements.txt", "Python"),
    ("package.json",  "TypeScript/Node"),
    ("*.ts",          "TypeScript/Node"),
    ("Dockerfile",    "Container"),
    ("*.jsonnet",     "Jsonnet"),
    ("*.libsonnet",   "Jsonnet"),
    ("*.cue",         "CUE"),
    ("kustomization.yaml", "Kustomize"),
    ("kustomization.yml",  "Kustomize"),
]

# Directories that are never repo roots
SKIP_DIRS = frozenset({
    ".git", ".hg", "node_modules", "vendor", ".vendor",
    ".terraform", "__pycache__", ".venv", "venv",
    "dist", "build", "_build",
})


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RepoSummary:
    """Structured knowledge extracted from a single git repo."""
    name: str                       # e.g. "alerting"
    domain: str                     # parent group dir, e.g. "Observability-Alerting"
    path: str                       # relative path from workspace root
    purpose: str                    # from README first paragraph
    tech_stack: list[str]           # ["Terraform", "Helm", "Go"]
    provides: list[str]             # what this repo delivers
    depends_on: list[dict]          # [{"type": "repo", "name": "...", "reason": "..."}]
    deployed_via: list[dict]        # [{"type": "argocd", "app": "...", "cluster": "..."}]
    key_files: list[str]            # most structurally important files
    ci_pipelines: list[str]         # CI workflow names
    owners: list[str]               # from moia.yml owners
    helm_chart_name: Optional[str]  # from Chart.yaml
    tf_backend_bucket: Optional[str] # from backend.tf
    go_module: Optional[str]        # from go.mod
    summary_text: str               # full prose for FTS5 + semantic indexing


@dataclass
class ExtractorStats:
    repos_found: int = 0
    repos_indexed: int = 0
    repos_updated: int = 0
    graph_nodes_created: int = 0
    graph_edges_created: int = 0
    errors: list = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────────────

def extract_and_index_repos(
    db: sqlite3.Connection,
    workspace: str,
) -> ExtractorStats:
    """Walk workspace/repos/, extract one RepoSummary per repo, write to DB.

    Args:
        db:        SQLite connection to knowledge.db
        workspace: Absolute path to the workspace root (parent of repos/)

    Returns:
        ExtractorStats with counts of what was created/updated.
    """
    stats = ExtractorStats()
    workspace_path = Path(workspace)
    repos_root = workspace_path / "repos"

    if not repos_root.is_dir():
        # Fall back: treat workspace root itself as repos root
        repos_root = workspace_path
        logger.warning("No repos/ subdirectory found; scanning workspace root: %s", workspace_path)

    summaries = discover_and_extract(repos_root, workspace_path)
    stats.repos_found = len(summaries)

    for summary in summaries:
        try:
            _index_repo_summary(db, summary, workspace, stats)
        except Exception as exc:
            stats.errors.append(f"{summary.path}: {exc}")
            logger.exception("Failed to index %s", summary.path)

    db.commit()
    logger.info(
        "Repo extraction complete: found=%d indexed=%d updated=%d edges=%d errors=%d",
        stats.repos_found, stats.repos_indexed, stats.repos_updated,
        stats.graph_edges_created, len(stats.errors),
    )
    return stats


def discover_and_extract(
    repos_root: Path,
    workspace_root: Optional[Path] = None,
) -> list[RepoSummary]:
    """Discover and extract RepoSummary objects without writing to DB.

    Useful for testing and inspection.
    """
    if workspace_root is None:
        workspace_root = repos_root.parent
    return list(_iter_repos(repos_root, workspace_root))


# ── Repo discovery ────────────────────────────────────────────────────────────

def _iter_repos(repos_root: Path, workspace_root: Path):
    """Yield RepoSummary for each detected repo under repos_root."""
    for domain_dir in sorted(repos_root.iterdir()):
        if not domain_dir.is_dir() or domain_dir.name.startswith("."):
            continue
        domain = domain_dir.name

        for candidate in sorted(domain_dir.iterdir()):
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            if candidate.name in SKIP_DIRS:
                continue

            if _is_repo_root(candidate):
                try:
                    summary = _extract_repo(candidate, domain, workspace_root)
                    yield summary
                except Exception as exc:
                    logger.warning("Skipping %s: %s", candidate, exc)


def _is_repo_root(path: Path) -> bool:
    """Return True if path looks like the root of a git repo / project."""
    entries = {p.name for p in path.iterdir() if not p.name.startswith(".")}
    entries |= {p.name for p in path.iterdir() if p.name.startswith(".")}
    return bool(entries & REPO_MARKERS)


# ── Per-repo extraction ───────────────────────────────────────────────────────

def _extract_repo(repo_path: Path, domain: str, workspace_root: Path) -> RepoSummary:
    """Build a RepoSummary by reading key files in repo_path."""
    name = repo_path.name
    rel_path = str(repo_path.relative_to(workspace_root))

    purpose = _extract_purpose(repo_path)
    tech_stack = _detect_tech_stack(repo_path)
    helm_chart_name, helm_deps = _extract_helm_info(repo_path)
    go_module = _extract_go_module(repo_path)
    tf_backend_bucket, tf_remote_state_deps = _extract_terraform_info(repo_path)
    python_deps = _extract_python_deps(repo_path)
    dockerfile_info = _extract_dockerfile_info(repo_path)
    ci_pipelines = _extract_ci_pipelines(repo_path)
    argocd_info = _extract_argocd_info(repo_path)
    owners = _extract_owners(repo_path)
    key_files = _detect_key_files(repo_path)

    # Aggregate depends_on from all sources
    depends_on: list[dict] = []
    for dep in tf_remote_state_deps:
        depends_on.append({"type": "repo", "name": dep, "reason": "terraform remote_state"})
    for dep in helm_deps:
        depends_on.append({"type": "chart", "name": dep, "reason": "helm dependency"})

    # deployed_via from argocd app/appset discovery
    deployed_via = argocd_info.get("deployed_via", [])

    # IRSA dependencies from ArgoCD values / k8s manifests
    irsa_roles = _extract_irsa_refs(repo_path)
    for role in irsa_roles:
        depends_on.append({"type": "iam_role", "name": role, "reason": "IRSA annotation"})

    # provides: heuristic based on tech stack + helm chart name
    provides = _infer_provides(name, tech_stack, helm_chart_name, argocd_info)

    summary_text = _build_summary_text(
        name=name,
        domain=domain,
        purpose=purpose,
        tech_stack=tech_stack,
        provides=provides,
        depends_on=depends_on,
        deployed_via=deployed_via,
        key_files=key_files,
        ci_pipelines=ci_pipelines,
        owners=owners,
        helm_chart_name=helm_chart_name,
        go_module=go_module,
        tf_backend_bucket=tf_backend_bucket,
        python_deps=python_deps,
        dockerfile_info=dockerfile_info,
    )

    return RepoSummary(
        name=name,
        domain=domain,
        path=rel_path,
        purpose=purpose,
        tech_stack=tech_stack,
        provides=provides,
        depends_on=depends_on,
        deployed_via=deployed_via,
        key_files=key_files,
        ci_pipelines=ci_pipelines,
        owners=owners,
        helm_chart_name=helm_chart_name,
        tf_backend_bucket=tf_backend_bucket,
        go_module=go_module,
        summary_text=summary_text,
    )


# ── File extractors ───────────────────────────────────────────────────────────

def _read_file(path: Path, max_bytes: int = 64 * 1024) -> Optional[str]:
    """Read a file safely; return None on error or oversized files."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return None


def _extract_purpose(repo_path: Path) -> str:
    """Extract the first meaningful paragraph from README.md."""
    for name in ("README.md", "readme.md", "README.rst", "README.txt"):
        readme = repo_path / name
        if readme.exists():
            content = _read_file(readme)
            if content:
                return _first_paragraph(content)
    return ""


def _first_paragraph(text: str) -> str:
    """Return the first non-header, non-empty paragraph (up to 400 chars)."""
    lines = text.split("\n")
    paragraph_lines: list[str] = []
    in_paragraph = False

    for line in lines:
        stripped = line.strip()
        # Skip badges, HTML, blank lines before paragraph starts
        if not stripped:
            if in_paragraph:
                break
            continue
        if stripped.startswith("#"):
            # Title line — skip but allow paragraph after
            continue
        if stripped.startswith("![") or stripped.startswith("<"):
            continue
        if stripped.startswith("***") or stripped.startswith("---"):
            if in_paragraph:
                break
            continue
        in_paragraph = True
        paragraph_lines.append(stripped)

    result = " ".join(paragraph_lines)
    # Truncate cleanly at sentence boundary if possible
    if len(result) > 400:
        truncated = result[:400]
        for sep in (". ", "! ", "? "):
            idx = truncated.rfind(sep)
            if idx > 100:
                return truncated[: idx + 1]
        return truncated + "..."
    return result


def _detect_tech_stack(repo_path: Path) -> list[str]:
    """Infer tech stack from file presence (non-recursive for speed)."""
    detected: list[str] = []
    seen: set[str] = set()

    def _add(tech: str) -> None:
        if tech not in seen:
            seen.add(tech)
            detected.append(tech)

    # Shallow scan of root + one level deep
    all_files: set[str] = set()
    for p in repo_path.iterdir():
        all_files.add(p.name)

    for sub in repo_path.iterdir():
        if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.name.startswith("."):
            try:
                for p in sub.iterdir():
                    all_files.add(p.name)
            except PermissionError:
                pass

    # Extension-based detection (walk up to 3 levels for *.tf, *.ts, etc.)
    extensions: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(repo_path)
        if len(rel.parts) > 3:
            dirnames.clear()
            continue
        for f in filenames:
            extensions.add(Path(f).suffix.lower())
            all_files.add(f)

    # Map to tech stack
    if ".tf" in extensions or "main.tf" in all_files or "versions.tf" in all_files:
        _add("Terraform")
    if "Chart.yaml" in all_files:
        _add("Helm")
    if "go.mod" in all_files:
        _add("Go")
    if "pyproject.toml" in all_files or "setup.py" in all_files or "requirements.txt" in all_files:
        _add("Python")
    if "package.json" in all_files:
        _add("TypeScript/Node")
    if ".ts" in extensions:
        _add("TypeScript/Node")
    if "Dockerfile" in all_files or ".dockerfile" in extensions:
        _add("Container")
    if ".jsonnet" in extensions or ".libsonnet" in extensions:
        _add("Jsonnet")
    if ".cue" in extensions:
        _add("CUE")
    if "kustomization.yaml" in all_files or "kustomization.yml" in all_files:
        _add("Kustomize")
    if ".yaml" in extensions or ".yml" in extensions:
        _add("YAML")  # fallback — always last

    return detected


def _extract_helm_info(repo_path: Path) -> tuple[Optional[str], list[str]]:
    """Return (chart_name, [dependency_names]) from Chart.yaml if present."""
    chart_yaml = repo_path / "Chart.yaml"
    if not chart_yaml.exists():
        # Look one level deep (charts/ subdirectory)
        for sub in repo_path.iterdir():
            if sub.is_dir() and (sub / "Chart.yaml").exists():
                chart_yaml = sub / "Chart.yaml"
                break
        else:
            return None, []

    content = _read_file(chart_yaml)
    if not content:
        return None, []

    name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
    chart_name = name_match.group(1).strip() if name_match else None

    # Extract dependency names from dependencies list
    deps = re.findall(r"^\s*-\s*name:\s*(.+)$", content, re.MULTILINE)
    dep_names = [d.strip() for d in deps if d.strip()]

    return chart_name, dep_names


def _extract_go_module(repo_path: Path) -> Optional[str]:
    """Return Go module path from go.mod."""
    go_mod = repo_path / "go.mod"
    if not go_mod.exists():
        return None
    content = _read_file(go_mod)
    if not content:
        return None
    match = re.search(r"^module\s+(\S+)", content, re.MULTILINE)
    return match.group(1) if match else None


def _extract_terraform_info(repo_path: Path) -> tuple[Optional[str], list[str]]:
    """Return (s3_backend_bucket, [remote_state_refs]) by scanning .tf files."""
    backend_bucket: Optional[str] = None
    remote_state_deps: list[str] = []
    seen_deps: set[str] = set()

    tf_files = _find_files(repo_path, "*.tf", max_depth=4)
    for tf_path in tf_files[:40]:  # cap at 40 files to avoid scanning huge repos
        content = _read_file(tf_path)
        if not content:
            continue

        # S3 backend bucket
        if backend_bucket is None:
            bucket_match = re.search(r'bucket\s*=\s*"([^"]+)"', content)
            if bucket_match and "backend" in content:
                backend_bucket = bucket_match.group(1)

        # terraform_remote_state data sources
        for match in re.finditer(
            r'data\s+"terraform_remote_state"\s+"([^"]+)"\s*\{[^}]*?config\s*=\s*\{[^}]*?key\s*=\s*"([^"]+)"',
            content, re.DOTALL
        ):
            key = match.group(2)
            dep_name = key.split("/")[0].rstrip("/")
            if dep_name and dep_name not in seen_deps:
                seen_deps.add(dep_name)
                remote_state_deps.append(dep_name)

        # Also match simpler pattern: backend key path as dependency hint
        for match in re.finditer(
            r'data\s+"terraform_remote_state"\s+"([^"]+)"',
            content
        ):
            alias = match.group(1)
            if alias not in seen_deps:
                seen_deps.add(alias)
                remote_state_deps.append(alias)

    return backend_bucket, remote_state_deps


def _extract_python_deps(repo_path: Path) -> list[str]:
    """Return top-level Python package names from pyproject.toml or requirements.txt."""
    deps: list[str] = []
    seen: set[str] = set()

    # pyproject.toml [project.dependencies]
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        content = _read_file(pyproject)
        if content:
            # Extract lines inside [project] dependencies array
            in_deps = False
            for line in content.split("\n"):
                if re.match(r"^\s*dependencies\s*=", line):
                    in_deps = True
                    continue
                if in_deps:
                    if line.strip().startswith("[") and not line.strip().startswith("[\""):
                        break
                    pkg_match = re.search(r'"([a-zA-Z0-9_-]+)', line)
                    if pkg_match:
                        pkg = pkg_match.group(1).lower()
                        if pkg not in seen:
                            seen.add(pkg)
                            deps.append(pkg)

    # requirements.txt
    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        content = _read_file(req_file)
        if content:
            for line in content.split("\n")[:30]:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    pkg = re.split(r"[>=<!~\[]", line)[0].strip().lower()
                    if pkg and pkg not in seen:
                        seen.add(pkg)
                        deps.append(pkg)

    return deps[:20]  # cap at 20


def _extract_dockerfile_info(repo_path: Path) -> dict:
    """Extract base image and exposed ports from Dockerfile."""
    dockerfile = repo_path / "Dockerfile"
    if not dockerfile.exists():
        return {}
    content = _read_file(dockerfile)
    if not content:
        return {}

    info: dict = {}
    from_match = re.search(r"^FROM\s+([^\s]+)", content, re.MULTILINE | re.IGNORECASE)
    if from_match:
        info["base_image"] = from_match.group(1)

    ports = re.findall(r"^EXPOSE\s+(\d+)", content, re.MULTILINE | re.IGNORECASE)
    if ports:
        info["exposed_ports"] = ports

    return info


def _extract_ci_pipelines(repo_path: Path) -> list[str]:
    """Return workflow names from .github/workflows/*.yml."""
    workflows_dir = repo_path / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []

    names: list[str] = []
    for wf_file in sorted(workflows_dir.glob("*.yml")):
        content = _read_file(wf_file)
        if not content:
            continue
        name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
        if name_match:
            names.append(name_match.group(1).strip())
        else:
            names.append(wf_file.stem)

    return names


def _extract_argocd_info(repo_path: Path) -> dict:
    """Scan YAML files for ArgoCD Application/ApplicationSet definitions."""
    deployed_via: list[dict] = []
    source_repos: list[str] = []
    seen: set[str] = set()

    yaml_files = _find_files(repo_path, "*.yaml", max_depth=4) + _find_files(repo_path, "*.yml", max_depth=4)

    for yf in yaml_files[:60]:
        content = _read_file(yf)
        if not content:
            continue
        if "argoproj.io" not in content and "argocd" not in content.lower():
            continue

        # kind: Application or ApplicationSet
        kind_match = re.search(r"^kind:\s*(Application(?:Set)?)", content, re.MULTILINE)
        if not kind_match:
            continue

        kind = kind_match.group(1)

        # App name
        name_match = re.search(r"^  name:\s*(.+)$", content, re.MULTILINE)
        app_name = name_match.group(1).strip() if name_match else Path(yf).stem

        # Destination server / cluster name
        dest_name_match = re.search(r"destination:.*?(?:name|server):\s*([^\n]+)", content, re.DOTALL)
        dest_server_matches = re.findall(r"destination:\s*\n(?:.*\n)*?\s*(?:name|server):\s*(\S+)", content)

        # Namespace
        ns_match = re.search(r"destination:.*?namespace:\s*(\S+)", content, re.DOTALL)
        namespace = ns_match.group(1).strip() if ns_match else None

        # Cluster names from common patterns
        cluster_matches = re.findall(r'"cluster-([a-z0-9_-]+)"', content)
        clusters = list(dict.fromkeys(cluster_matches))[:5]  # deduplicate, preserve order

        # repoURL references
        repo_urls = re.findall(r"repoURL:\s*['\"]?([^\s'\"]+)", content)
        for url in repo_urls:
            repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
            if repo_name and repo_name not in source_repos:
                source_repos.append(repo_name)

        key = f"{kind}:{app_name}"
        if key not in seen:
            seen.add(key)
            entry: dict = {"type": kind.lower(), "app": app_name}
            if clusters:
                entry["clusters"] = clusters
            if namespace:
                entry["namespace"] = namespace
            deployed_via.append(entry)

    return {
        "deployed_via": deployed_via,
        "source_repos": source_repos,
    }


def _extract_irsa_refs(repo_path: Path) -> list[str]:
    """Find IAM role ARNs from IRSA annotations in YAML files."""
    roles: list[str] = []
    seen: set[str] = set()

    yaml_files = _find_files(repo_path, "*.yaml", max_depth=4) + _find_files(repo_path, "*.yml", max_depth=4)
    for yf in yaml_files[:60]:
        content = _read_file(yf)
        if not content or "eks.amazonaws.com/role-arn" not in content:
            continue
        for match in re.finditer(r"eks\.amazonaws\.com/role-arn[\"']?\s*:\s*[\"']?([^\s\"'\n]+)", content):
            arn = match.group(1).strip()
            if arn not in seen:
                seen.add(arn)
                # Shorten ARN to just the role name for readability
                role_name = arn.split("/")[-1] if "/" in arn else arn
                roles.append(role_name)

    return roles[:10]


def _extract_owners(repo_path: Path) -> list[str]:
    """Extract owner teams from moia.yml."""
    moia_yml = repo_path / "moia.yml"
    if not moia_yml.exists():
        return []
    content = _read_file(moia_yml)
    if not content:
        return []
    owners = re.findall(r"^\s*-\s*(.+)$", content[content.find("owners:"):], re.MULTILINE)
    return [o.strip() for o in owners if o.strip() and not o.strip().startswith("#")][:5]


def _detect_key_files(repo_path: Path) -> list[str]:
    """Return the most structurally significant files in the repo."""
    candidates = [
        "README.md", "Chart.yaml", "go.mod", "pyproject.toml", "Dockerfile",
        "main.tf", "versions.tf", "backend.tf", "moia.yml",
        "kustomization.yaml", "kustomization.yml",
    ]
    found: list[str] = []
    for name in candidates:
        if (repo_path / name).exists():
            found.append(name)

    # Add CI workflow files
    wf_dir = repo_path / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("*.yml"))[:3]:
            found.append(f".github/workflows/{wf.name}")

    return found


# ── Summary text builder ──────────────────────────────────────────────────────

def _infer_provides(
    name: str,
    tech_stack: list[str],
    helm_chart_name: Optional[str],
    argocd_info: dict,
) -> list[str]:
    """Heuristic: what does this repo offer?"""
    provides: list[str] = []

    if "Helm" in tech_stack:
        chart = helm_chart_name or name
        provides.append(f"Helm chart: {chart}")
    if "Terraform" in tech_stack:
        provides.append("Terraform infrastructure modules")
    if "Go" in tech_stack:
        provides.append("Go service/operator")
    if "Python" in tech_stack:
        provides.append("Python service")
    if "Container" in tech_stack and "Go" not in tech_stack and "Python" not in tech_stack:
        provides.append("Containerised application")
    if argocd_info.get("deployed_via"):
        apps = [d.get("app", "") for d in argocd_info["deployed_via"]]
        provides.append(f"ArgoCD applications: {', '.join(a for a in apps if a)}")

    return provides


def _build_summary_text(
    name: str,
    domain: str,
    purpose: str,
    tech_stack: list[str],
    provides: list[str],
    depends_on: list[dict],
    deployed_via: list[dict],
    key_files: list[str],
    ci_pipelines: list[str],
    owners: list[str],
    helm_chart_name: Optional[str],
    go_module: Optional[str],
    tf_backend_bucket: Optional[str],
    python_deps: list[str],
    dockerfile_info: dict,
) -> str:
    """Build a natural-language paragraph for FTS5 + semantic indexing."""
    parts: list[str] = []

    # Header
    header = f"{name} ({domain})"
    if purpose:
        header += f": {purpose}"
    parts.append(header)

    # Tech stack
    if tech_stack:
        parts.append(f"Tech stack: {', '.join(tech_stack)}.")

    # Specific runtime details
    if go_module:
        parts.append(f"Go module: {go_module}.")
    if helm_chart_name:
        parts.append(f"Helm chart: {helm_chart_name}.")
    if tf_backend_bucket:
        parts.append(f"Terraform state bucket: {tf_backend_bucket}.")
    if dockerfile_info.get("base_image"):
        parts.append(f"Container base image: {dockerfile_info['base_image']}.")
    if python_deps:
        parts.append(f"Python dependencies: {', '.join(python_deps[:10])}.")

    # Deployment info
    if deployed_via:
        apps = [d.get("app", "") for d in deployed_via if d.get("app")]
        clusters_all: list[str] = []
        for d in deployed_via:
            clusters_all.extend(d.get("clusters", []))
        cluster_text = f" to clusters: {', '.join(dict.fromkeys(clusters_all))}" if clusters_all else ""
        if apps:
            parts.append(f"Deployed via ArgoCD{cluster_text}. Applications: {', '.join(apps[:5])}.")
        else:
            parts.append(f"Deployed via ArgoCD{cluster_text}.")

    # Dependencies
    if depends_on:
        dep_texts: list[str] = []
        for dep in depends_on[:8]:
            reason = dep.get("reason", "")
            dep_name = dep.get("name", "")
            dep_texts.append(f"{dep_name} ({reason})" if reason else dep_name)
        parts.append(f"Depends on: {', '.join(dep_texts)}.")

    # Provides
    if provides:
        parts.append(f"Provides: {', '.join(provides)}.")

    # CI
    if ci_pipelines:
        parts.append(f"CI pipelines: {', '.join(ci_pipelines[:5])}.")

    # Owners
    if owners:
        parts.append(f"Owners: {', '.join(owners)}.")

    # Key files
    if key_files:
        parts.append(f"Key files: {', '.join(key_files[:8])}.")

    return " ".join(parts)


# ── Database persistence ──────────────────────────────────────────────────────

def _index_repo_summary(
    db: sqlite3.Connection,
    summary: RepoSummary,
    workspace: str,
    stats: ExtractorStats,
) -> None:
    """Upsert a repo summary into knowledge_entries and create graph edges."""
    content = summary.summary_text
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
    title = f"{summary.name} ({summary.domain})"
    source_path = f"repos/{summary.path}"
    now = datetime.now(timezone.utc).isoformat()

    metadata = {
        "repo_name": summary.name,
        "domain": summary.domain,
        "tech_stack": summary.tech_stack,
        "owners": summary.owners,
        "helm_chart": summary.helm_chart_name,
        "go_module": summary.go_module,
        "tf_backend": summary.tf_backend_bucket,
        "synced_at": now,
    }

    # Check for existing entry
    existing = db.execute(
        "SELECT id, content_hash FROM knowledge_entries WHERE workspace = ? AND source_path = ?",
        (workspace, source_path)
    ).fetchone()

    if existing:
        if existing[1] == content_hash:
            # Unchanged — still create/update graph edges
            entry_id = existing[0]
        else:
            db.execute(
                """UPDATE knowledge_entries
                   SET content = ?, content_hash = ?, title = ?, content_type = ?,
                       updated_at = ?, metadata = ?
                   WHERE id = ?""",
                (content, content_hash, title, "repo_summary", now,
                 json.dumps(metadata), existing[0])
            )
            entry_id = existing[0]
            stats.repos_updated += 1
    else:
        entry_uuid = str(_uuid.uuid4())
        db.execute(
            """INSERT INTO knowledge_entries
               (uuid, workspace, source_path, source_type, content_type, title, content, content_hash, metadata)
               VALUES (?, ?, ?, 'repo', 'repo_summary', ?, ?, ?, ?)""",
            (entry_uuid, workspace, source_path, title, content, content_hash,
             json.dumps(metadata))
        )
        row = db.execute(
            "SELECT id FROM knowledge_entries WHERE uuid = ?", (entry_uuid,)
        ).fetchone()
        entry_id = row[0] if row else None
        stats.repos_indexed += 1

        # Queue for embedding
        if entry_id:
            try:
                db.execute(
                    "INSERT INTO embedding_queue (entry_id) VALUES (?)", (entry_id,)
                )
            except Exception:
                pass  # embedding_queue may not exist in test environments

    if entry_id is None:
        return

    # Create graph nodes and edges
    _create_graph_edges(db, summary, workspace, entry_id, stats)


def _create_graph_edges(
    db: sqlite3.Connection,
    summary: RepoSummary,
    workspace: str,
    entry_id: int,
    stats: ExtractorStats,
) -> None:
    """Create knowledge graph nodes and edges for a repo."""
    repo_name = summary.name
    domain = summary.domain

    def _edge(src_name, src_type, tgt_name, tgt_type, predicate, meta=None):
        try:
            add_edge(
                db,
                source_name=src_name,
                source_type=src_type,
                target_name=tgt_name,
                target_type=tgt_type,
                predicate=predicate,
                workspace=workspace,
                metadata=meta,
            )
            stats.graph_edges_created += 1
        except Exception as exc:
            logger.debug("Edge creation failed %s -[%s]-> %s: %s", src_name, predicate, tgt_name, exc)

    # Repo belongs_to domain
    _edge(repo_name, "repo", domain, "domain", "belongs_to_domain")

    # Repo has tech stack
    for tech in summary.tech_stack:
        _edge(repo_name, "repo", tech, "technology", "uses_technology")

    # Repo depends_on others
    for dep in summary.depends_on:
        dep_name = dep.get("name", "")
        dep_type = dep.get("type", "repo")
        reason = dep.get("reason", "")
        if dep_name:
            _edge(
                repo_name, "repo",
                dep_name, dep_type,
                "depends_on",
                meta={"reason": reason} if reason else None,
            )

    # Deployed via ArgoCD
    for deployment in summary.deployed_via:
        app_name = deployment.get("app", "")
        if app_name:
            _edge(repo_name, "repo", app_name, "argocd_app", "deployed_by")
        for cluster in deployment.get("clusters", []):
            _edge(repo_name, "repo", cluster, "cluster", "deployed_to")

    # Helm chart provides
    if summary.helm_chart_name:
        _edge(repo_name, "repo", summary.helm_chart_name, "helm_chart", "provides_chart")

    # Owners
    for owner in summary.owners:
        _edge(owner, "team", repo_name, "repo", "owns")

    # CI pipelines
    for pipeline in summary.ci_pipelines:
        _edge(repo_name, "repo", pipeline, "ci_pipeline", "has_pipeline")

    # Link the repo node to its knowledge_entry so graph retrieval can resolve
    # entry_id.  Without this, get_related_entries_with_depth always returns
    # empty because it looks for metadata->entry_id on every traversed node.
    repo_node_id = _uuid.uuid5(
        _uuid.NAMESPACE_URL,
        f"{workspace}::{repo_name}".lower().replace(" ", "_"),
    ).hex
    try:
        db.execute(
            "UPDATE knowledge_graph_nodes SET metadata = ? WHERE id = ?",
            (json.dumps({"entry_id": entry_id}), repo_node_id),
        )
        db.commit()
        logger.debug("Set entry_id=%s on repo node %s (%s)", entry_id, repo_name, repo_node_id)
    except Exception as exc:
        logger.debug("Failed to set entry_id on repo node %s: %s", repo_name, exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_files(root: Path, pattern: str, max_depth: int = 3) -> list[Path]:
    """Recursively find files matching pattern up to max_depth levels deep."""
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) > max_depth:
            dirnames.clear()
            continue
        import fnmatch
        for fname in filenames:
            if fnmatch.fnmatch(fname, pattern):
                results.append(Path(dirpath) / fname)
    return results

"""Blast radius pre-assessment.

Before executing a change, estimates what systems, services, and users
could be affected. Uses the knowledge graph to trace dependencies and
the git history to identify high-churn areas.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ImpactZone:
    name: str
    impact_type: str  # "direct", "transitive", "potential"
    confidence: float = 1.0
    services_affected: List[str] = field(default_factory=list)
    files_affected: List[str] = field(default_factory=list)
    teams_affected: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)


@dataclass
class BlastRadiusAssessment:
    target: str
    change_type: str
    scope: str  # "single_file", "module", "service", "cross_service"
    risk_level: str  # "low", "medium", "high", "critical"
    impact_zones: List[ImpactZone] = field(default_factory=list)
    total_services_affected: int = 0
    total_files_affected: int = 0
    total_teams_affected: int = 0
    recommendations: List[str] = field(default_factory=list)
    requires_approval: bool = False
    approval_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "change_type": self.change_type,
            "scope": self.scope,
            "risk_level": self.risk_level,
            "impact_zones": [
                {
                    "name": z.name,
                    "impact_type": z.impact_type,
                    "confidence": z.confidence,
                    "services_affected": z.services_affected,
                    "files_affected": z.files_affected,
                    "teams_affected": z.teams_affected,
                    "risk_factors": z.risk_factors,
                }
                for z in self.impact_zones
            ],
            "total_services_affected": self.total_services_affected,
            "total_files_affected": self.total_files_affected,
            "total_teams_affected": self.total_teams_affected,
            "recommendations": self.recommendations,
            "requires_approval": self.requires_approval,
            "approval_reason": self.approval_reason,
        }


def assess_blast_radius(
    knowledge_db: sqlite3.Connection,
    target: str,
    change_type: str = "modify",
    workspace: Optional[str] = None,
) -> BlastRadiusAssessment:
    """Assess the blast radius of a proposed change using the knowledge graph.

    Uses graph edges to find:
    - Direct dependents (services that import/depend on the target)
    - Transitive dependents (services depending on direct dependents)
    - Team ownership (who owns affected services)
    """
    impact_zones: List[ImpactZone] = []
    services = set()
    files = set()
    teams = set()

    # Find direct dependents via graph edges
    direct_deps = _find_dependents(knowledge_db, target, workspace, depth=1)
    if direct_deps:
        zone = ImpactZone(
            name="direct_dependents",
            impact_type="direct",
            confidence=0.95,
            services_affected=direct_deps["services"],
            files_affected=direct_deps["files"],
            teams_affected=direct_deps["teams"],
        )
        impact_zones.append(zone)
        services.update(direct_deps["services"])
        files.update(direct_deps["files"])
        teams.update(direct_deps["teams"])

    # Find transitive dependents (depth 2)
    transitive_deps = _find_dependents(knowledge_db, target, workspace, depth=2)
    transitive_only = {
        "services": [s for s in transitive_deps.get("services", []) if s not in services],
        "files": [f for f in transitive_deps.get("files", []) if f not in files],
        "teams": [t for t in transitive_deps.get("teams", []) if t not in teams],
    }
    if any(transitive_only.values()):
        zone = ImpactZone(
            name="transitive_dependents",
            impact_type="transitive",
            confidence=0.7,
            services_affected=transitive_only["services"],
            files_affected=transitive_only["files"],
            teams_affected=transitive_only["teams"],
            risk_factors=["Indirect dependency — changes may propagate unexpectedly"],
        )
        impact_zones.append(zone)
        services.update(transitive_only["services"])
        files.update(transitive_only["files"])
        teams.update(transitive_only["teams"])

    # Determine scope and risk
    scope = _determine_scope(len(services), len(files))
    risk_level = _determine_risk(scope, change_type, len(teams))

    recommendations = _generate_recommendations(scope, risk_level, change_type, len(services))

    requires_approval = risk_level in ("high", "critical") or len(teams) > 1
    approval_reason = ""
    if requires_approval:
        if len(teams) > 1:
            approval_reason = f"Change affects {len(teams)} teams: {', '.join(sorted(teams)[:5])}"
        elif risk_level == "critical":
            approval_reason = f"Critical risk: {len(services)} services affected"
        else:
            approval_reason = f"High risk: cross-service change"

    return BlastRadiusAssessment(
        target=target,
        change_type=change_type,
        scope=scope,
        risk_level=risk_level,
        impact_zones=impact_zones,
        total_services_affected=len(services),
        total_files_affected=len(files),
        total_teams_affected=len(teams),
        recommendations=recommendations,
        requires_approval=requires_approval,
        approval_reason=approval_reason,
    )


def _find_dependents(
    db: sqlite3.Connection,
    target: str,
    workspace: Optional[str],
    depth: int,
) -> Dict[str, List[str]]:
    """Walk the knowledge graph to find entities depending on the target."""
    services: List[str] = []
    files: List[str] = []
    teams: List[str] = []

    # Find the target node
    ws_filter = "AND workspace = ?" if workspace else ""
    ws_params = (workspace,) if workspace else ()

    target_nodes = db.execute(
        f"SELECT id, entity_type FROM knowledge_graph_nodes WHERE entity_name LIKE ? {ws_filter}",
        (f"%{target}%", *ws_params),
    ).fetchall()

    if not target_nodes:
        return {"services": services, "files": files, "teams": teams}

    visited = set()
    frontier = [node_id for node_id, _ in target_nodes]

    for _ in range(depth):
        if not frontier:
            break
        next_frontier = []
        placeholders = ",".join("?" * len(frontier))
        edges = db.execute(
            f"""SELECT source_id, target_id, predicate FROM knowledge_graph_edges
                WHERE target_id IN ({placeholders}) AND source_id NOT IN ({placeholders})""",
            (*frontier, *frontier),
        ).fetchall()

        for source_id, _, predicate in edges:
            if source_id in visited:
                continue
            visited.add(source_id)
            next_frontier.append(source_id)

            node = db.execute(
                "SELECT entity_name, entity_type FROM knowledge_graph_nodes WHERE id = ?",
                (source_id,),
            ).fetchone()
            if node:
                name, entity_type = node
                if entity_type in ("service", "repo", "application"):
                    services.append(name)
                elif entity_type == "file":
                    files.append(name)
                elif entity_type == "team":
                    teams.append(name)

        frontier = next_frontier

    # Also look for ownership edges
    for node_id, _ in target_nodes:
        owner_rows = db.execute(
            """SELECT n.entity_name FROM knowledge_graph_edges e
               JOIN knowledge_graph_nodes n ON n.id = e.source_id
               WHERE e.target_id = ? AND e.predicate IN ('owns', 'maintains', 'responsible_for')""",
            (node_id,),
        ).fetchall()
        for (name,) in owner_rows:
            if name not in teams:
                teams.append(name)

    return {"services": services, "files": files, "teams": teams}


def _determine_scope(service_count: int, file_count: int) -> str:
    if service_count == 0 and file_count <= 1:
        return "single_file"
    elif service_count <= 1:
        return "module"
    elif service_count <= 3:
        return "service"
    else:
        return "cross_service"


def _determine_risk(scope: str, change_type: str, team_count: int) -> str:
    base_risk = {"single_file": 0, "module": 1, "service": 2, "cross_service": 3}[scope]
    type_modifier = {"read": -1, "modify": 0, "delete": 1, "create": 0, "refactor": 1}.get(change_type, 0)
    team_modifier = 1 if team_count > 1 else 0

    total = base_risk + type_modifier + team_modifier
    if total <= 0:
        return "low"
    elif total <= 1:
        return "medium"
    elif total <= 3:
        return "high"
    else:
        return "critical"


def _generate_recommendations(scope: str, risk_level: str, change_type: str, service_count: int) -> List[str]:
    recs = []
    if risk_level in ("high", "critical"):
        recs.append("Run full integration test suite before merging")
    if service_count > 2:
        recs.append(f"Notify owners of {service_count} affected services before proceeding")
    if change_type == "delete":
        recs.append("Verify no runtime references exist (grep for dynamic imports/env vars)")
    if scope == "cross_service":
        recs.append("Consider phased rollout to minimize blast radius")
        recs.append("Add feature flag or backwards-compatible transition period")
    if risk_level == "critical":
        recs.append("Requires PO approval before execution")
    return recs

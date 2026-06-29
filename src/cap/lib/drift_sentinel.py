"""Drift detection sentinel.

Runs terraform plan on configured workspaces periodically and records
drift findings in the backlog for triage. Designed to be invoked by
cron, launchd, or the workflow engine on a schedule.

Usage:
    python -m cap.lib.drift_sentinel --workspace /path/to/tf --profile pe-readonly

Or from CAP CLI:
    cap drift check --workspace /path/to/tf
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DriftFinding:
    resource_address: str
    change_type: str  # "update", "create", "delete", "replace"
    attributes_changed: List[str] = field(default_factory=list)
    before: Optional[str] = None
    after: Optional[str] = None


@dataclass
class DriftReport:
    workspace: str
    profile: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    has_drift: bool = False
    findings: List[DriftFinding] = field(default_factory=list)
    plan_exit_code: int = 0
    plan_stderr: str = ""
    duration_seconds: float = 0.0

    @property
    def summary(self) -> str:
        if not self.has_drift:
            return "No drift detected"
        changes = {"update": 0, "create": 0, "delete": 0, "replace": 0}
        for f in self.findings:
            changes[f.change_type] = changes.get(f.change_type, 0) + 1
        parts = [f"{v} {k}" for k, v in changes.items() if v > 0]
        return f"Drift detected: {', '.join(parts)}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace": self.workspace,
            "profile": self.profile,
            "timestamp": self.timestamp,
            "has_drift": self.has_drift,
            "summary": self.summary,
            "findings": [
                {
                    "resource_address": f.resource_address,
                    "change_type": f.change_type,
                    "attributes_changed": f.attributes_changed,
                }
                for f in self.findings
            ],
            "plan_exit_code": self.plan_exit_code,
            "duration_seconds": self.duration_seconds,
        }


def check_drift(
    workspace: str,
    profile: str = "",
    terraform_bin: str = "terraform",
) -> DriftReport:
    """Run terraform plan -detailed-exitcode and parse drift.

    Exit codes:
      0 = no changes (no drift)
      1 = error
      2 = changes detected (drift)
    """
    import time

    ws_path = Path(workspace)
    if not ws_path.exists():
        return DriftReport(
            workspace=workspace, profile=profile,
            plan_exit_code=-1, plan_stderr=f"Workspace not found: {workspace}",
        )

    start = time.time()

    cmd = [terraform_bin, "plan", "-detailed-exitcode", "-no-color", "-input=false"]
    env_additions = {}
    if profile:
        env_additions["AWS_PROFILE"] = profile

    import os
    env = {**os.environ, **env_additions}

    try:
        result = subprocess.run(
            cmd,
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return DriftReport(
            workspace=workspace, profile=profile,
            plan_exit_code=-2, plan_stderr="Terraform plan timed out (300s)",
            duration_seconds=300.0,
        )
    except FileNotFoundError:
        return DriftReport(
            workspace=workspace, profile=profile,
            plan_exit_code=-3, plan_stderr=f"terraform binary not found: {terraform_bin}",
        )

    duration = time.time() - start

    report = DriftReport(
        workspace=workspace,
        profile=profile,
        plan_exit_code=result.returncode,
        plan_stderr=result.stderr[:2000] if result.stderr else "",
        duration_seconds=round(duration, 2),
    )

    if result.returncode == 2:
        report.has_drift = True
        report.findings = _parse_plan_output(result.stdout)
    elif result.returncode == 1:
        report.plan_stderr = result.stderr[:2000]

    return report


def _parse_plan_output(stdout: str) -> List[DriftFinding]:
    """Parse terraform plan stdout for resource changes.

    Looks for lines like:
      # aws_iam_role.example will be updated in-place
      # aws_s3_bucket.logs will be created
      # aws_security_group.old will be destroyed
    """
    findings: List[DriftFinding] = []
    change_markers = {
        "will be updated in-place": "update",
        "will be created": "create",
        "will be destroyed": "delete",
        "must be replaced": "replace",
    }

    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            for marker, change_type in change_markers.items():
                if marker in stripped:
                    resource = stripped.split("#")[1].split(" will ")[0].split(" must ")[0].strip()
                    findings.append(DriftFinding(
                        resource_address=resource,
                        change_type=change_type,
                    ))
                    break

    return findings


def check_drift_and_report(
    workspace: str,
    profile: str = "",
    backlog_db_path: Optional[str] = None,
) -> DriftReport:
    """Check drift and optionally create a backlog task if drift is found."""
    report = check_drift(workspace, profile)

    if report.has_drift and backlog_db_path:
        import sqlite3
        from cap.lib.backlog import init_backlog_table, create_task, BacklogTask, TaskPriority, TaskStatus

        db = sqlite3.connect(backlog_db_path)
        db.execute("PRAGMA busy_timeout=5000")
        init_backlog_table(db)

        task = BacklogTask(
            title=f"Drift detected: {report.summary}",
            description=json.dumps(report.to_dict(), indent=2),
            priority=TaskPriority.high,
            status=TaskStatus.ready,
            labels=["drift", "terraform", "auto-detected"],
            created_by="drift-sentinel",
        )
        create_task(db, task)
        db.close()

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CAP Drift Detection Sentinel")
    parser.add_argument("--workspace", "-w", required=True, help="Terraform workspace path")
    parser.add_argument("--profile", "-p", default="", help="AWS profile")
    parser.add_argument("--backlog-db", default=None, help="Path to backlog.db for auto-task creation")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    report = check_drift_and_report(args.workspace, args.profile, args.backlog_db)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        if report.has_drift:
            print(f"DRIFT DETECTED in {report.workspace}")
            print(f"  {report.summary}")
            for f in report.findings:
                print(f"  - {f.resource_address} ({f.change_type})")
        elif report.plan_exit_code == 0:
            print(f"No drift in {report.workspace}")
        else:
            print(f"ERROR checking {report.workspace}: exit {report.plan_exit_code}")
            if report.plan_stderr:
                print(f"  {report.plan_stderr[:200]}")

    sys.exit(0 if not report.has_drift else 2)

"""Tests for drift detection sentinel."""
import pytest
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.drift_sentinel import (
    DriftFinding, DriftReport, check_drift, _parse_plan_output,
    check_drift_and_report,
)


SAMPLE_PLAN_OUTPUT = """\
Terraform will perform the following actions:

  # aws_iam_role.lambda_exec will be updated in-place
  ~ resource "aws_iam_role" "lambda_exec" {
        id   = "lambda-exec-role"
        name = "lambda-exec-role"
      ~ assume_role_policy = jsonencode(...)
    }

  # aws_s3_bucket.logs will be created
  + resource "aws_s3_bucket" "logs" {
        + bucket = "my-logs-bucket"
    }

  # aws_security_group.old will be destroyed
  - resource "aws_security_group" "old" {
        - id   = "sg-12345"
    }

  # aws_instance.web must be replaced
  -/+ resource "aws_instance" "web" {
        ~ ami = "ami-old" -> "ami-new"
    }

Plan: 1 to add, 1 to change, 1 to destroy.
"""


def test_parse_plan_output():
    findings = _parse_plan_output(SAMPLE_PLAN_OUTPUT)
    assert len(findings) == 4

    types = {f.change_type for f in findings}
    assert types == {"update", "create", "delete", "replace"}

    addresses = {f.resource_address for f in findings}
    assert "aws_iam_role.lambda_exec" in addresses
    assert "aws_s3_bucket.logs" in addresses
    assert "aws_security_group.old" in addresses
    assert "aws_instance.web" in addresses


def test_parse_empty_output():
    findings = _parse_plan_output("")
    assert findings == []


def test_parse_no_drift_output():
    findings = _parse_plan_output("No changes. Your infrastructure matches the configuration.")
    assert findings == []


def test_drift_report_summary():
    report = DriftReport(
        workspace="/tf/prod",
        profile="prod",
        has_drift=True,
        findings=[
            DriftFinding(resource_address="aws_iam_role.x", change_type="update"),
            DriftFinding(resource_address="aws_s3_bucket.y", change_type="create"),
            DriftFinding(resource_address="aws_sg.z", change_type="delete"),
        ],
    )
    summary = report.summary
    assert "1 update" in summary
    assert "1 create" in summary
    assert "1 delete" in summary


def test_drift_report_no_drift_summary():
    report = DriftReport(workspace="/tf/staging", profile="staging", has_drift=False)
    assert report.summary == "No drift detected"


def test_drift_report_serialization():
    report = DriftReport(
        workspace="/tf/prod",
        profile="prod",
        has_drift=True,
        plan_exit_code=2,
        findings=[DriftFinding(resource_address="aws_s3.x", change_type="update")],
    )
    d = report.to_dict()
    assert d["workspace"] == "/tf/prod"
    assert d["has_drift"] is True
    assert len(d["findings"]) == 1
    assert d["findings"][0]["resource_address"] == "aws_s3.x"


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_no_changes(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout="No changes.", stderr=""
    )
    tmp = tempfile.mkdtemp()
    report = check_drift(tmp, profile="test")
    assert report.has_drift is False
    assert report.plan_exit_code == 0


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_with_changes(mock_run):
    mock_run.return_value = MagicMock(
        returncode=2, stdout=SAMPLE_PLAN_OUTPUT, stderr=""
    )
    tmp = tempfile.mkdtemp()
    report = check_drift(tmp, profile="test")
    assert report.has_drift is True
    assert len(report.findings) == 4


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_error(mock_run):
    mock_run.return_value = MagicMock(
        returncode=1, stdout="", stderr="Error: backend not initialized"
    )
    tmp = tempfile.mkdtemp()
    report = check_drift(tmp, profile="test")
    assert report.has_drift is False
    assert report.plan_exit_code == 1
    assert "backend" in report.plan_stderr


def test_check_drift_missing_workspace():
    report = check_drift("/nonexistent/path/that/does/not/exist")
    assert report.plan_exit_code == -1
    assert "not found" in report.plan_stderr.lower()


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_timeout(mock_run):
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="terraform", timeout=300)
    tmp = tempfile.mkdtemp()
    report = check_drift(tmp)
    assert report.plan_exit_code == -2
    assert "timed out" in report.plan_stderr.lower()


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_binary_not_found(mock_run):
    mock_run.side_effect = FileNotFoundError()
    tmp = tempfile.mkdtemp()
    report = check_drift(tmp, terraform_bin="terraform-v99")
    assert report.plan_exit_code == -3


@patch("cap.lib.drift_sentinel.subprocess.run")
def test_check_drift_and_report_creates_backlog_task(mock_run):
    mock_run.return_value = MagicMock(
        returncode=2, stdout=SAMPLE_PLAN_OUTPUT, stderr=""
    )
    tmp = tempfile.mkdtemp()
    ws_dir = Path(tmp) / "tf"
    ws_dir.mkdir()
    db_path = str(Path(tmp) / "backlog.db")

    report = check_drift_and_report(str(ws_dir), profile="test", backlog_db_path=db_path)
    assert report.has_drift is True

    # Verify task was created in backlog
    import sqlite3
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT title, labels FROM backlog_tasks").fetchall()
    assert len(rows) == 1
    assert "Drift detected" in rows[0][0]
    assert "drift" in rows[0][1]
    conn.close()

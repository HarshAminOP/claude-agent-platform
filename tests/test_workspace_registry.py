"""Unit tests for cap.lib.workspace_registry.

All tests are fully offline — no filesystem side-effects outside tmp_path.
The registry config path is monkey-patched to a temp file so real
harness-config.json is never touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.workspace_registry import (
    add_endpoint,
    add_workspace,
    get_endpoint,
    get_workspace,
    list_endpoints,
    list_workspaces,
    load_config,
    mark_workspace_synced,
    remove_endpoint,
    remove_workspace,
    save_config,
    update_workspace,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_config_path(tmp_path, monkeypatch):
    """Redirect all registry reads/writes to a temporary file."""
    config_file = tmp_path / "harness-config.json"

    def _fake_path():
        return config_file

    monkeypatch.setattr(
        "cap.lib.workspace_registry._config_path",
        _fake_path,
    )
    yield config_file


# ---------------------------------------------------------------------------
# load_config / save_config
# ---------------------------------------------------------------------------


def test_load_config_missing_file():
    """load_config returns {} when the file does not exist."""
    cfg = load_config()
    assert cfg == {}


def test_save_and_load_roundtrip(tmp_path):
    """save_config followed by load_config returns identical data."""
    data = {"provider": "aws-bedrock", "workspaces": [{"path": "/tmp/foo"}]}
    save_config(data)
    assert load_config() == data


def test_save_config_creates_parents(tmp_path, monkeypatch):
    """save_config creates missing parent directories."""
    nested = tmp_path / "deep" / "nested" / "harness-config.json"
    monkeypatch.setattr("cap.lib.workspace_registry._config_path", lambda: nested)
    save_config({"x": 1})
    assert nested.exists()
    assert json.loads(nested.read_text()) == {"x": 1}


# ---------------------------------------------------------------------------
# list_workspaces
# ---------------------------------------------------------------------------


def test_list_workspaces_empty():
    """list_workspaces returns [] when config has no workspaces key."""
    assert list_workspaces() == []


def test_list_workspaces_returns_entries():
    """list_workspaces returns all registered entries."""
    save_config({"workspaces": [{"path": "/a"}, {"path": "/b"}]})
    result = list_workspaces()
    assert len(result) == 2
    assert result[0]["path"] == "/a"


# ---------------------------------------------------------------------------
# add_workspace
# ---------------------------------------------------------------------------


def test_add_workspace_creates_entry(tmp_path):
    """add_workspace registers a new directory."""
    ws_dir = tmp_path / "myrepo"
    ws_dir.mkdir()
    entry = add_workspace(str(ws_dir))
    assert entry["path"] == str(ws_dir.resolve())
    assert entry["auto_added"] is False
    assert entry["sync_frequency"] == "5m"
    assert len(list_workspaces()) == 1


def test_add_workspace_idempotent(tmp_path):
    """add_workspace does not create duplicate entries."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    add_workspace(str(ws_dir))
    add_workspace(str(ws_dir))
    assert len(list_workspaces()) == 1


def test_add_workspace_auto_added_flag(tmp_path):
    """add_workspace sets auto_added=True when requested."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    entry = add_workspace(str(ws_dir), auto_added=True)
    assert entry["auto_added"] is True


def test_add_workspace_custom_settings(tmp_path):
    """add_workspace stores overridden sync_frequency."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    entry = add_workspace(str(ws_dir), sync_frequency="1h")
    assert entry["sync_frequency"] == "1h"


# ---------------------------------------------------------------------------
# get_workspace
# ---------------------------------------------------------------------------


def test_get_workspace_existing(tmp_path):
    """get_workspace returns the matching entry."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    add_workspace(str(ws_dir))
    result = get_workspace(str(ws_dir))
    assert result is not None
    assert result["path"] == str(ws_dir.resolve())


def test_get_workspace_missing():
    """get_workspace returns None for unknown paths."""
    assert get_workspace("/nonexistent/path/xyz") is None


# ---------------------------------------------------------------------------
# remove_workspace
# ---------------------------------------------------------------------------


def test_remove_workspace_existing(tmp_path):
    """remove_workspace removes the entry and returns True."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    add_workspace(str(ws_dir))
    result = remove_workspace(str(ws_dir))
    assert result is True
    assert list_workspaces() == []


def test_remove_workspace_missing():
    """remove_workspace returns False when path is not registered."""
    result = remove_workspace("/not/registered")
    assert result is False


# ---------------------------------------------------------------------------
# update_workspace
# ---------------------------------------------------------------------------


def test_update_workspace_updates_field(tmp_path):
    """update_workspace changes the specified field."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    add_workspace(str(ws_dir))
    updated = update_workspace(str(ws_dir), sync_frequency="10m")
    assert updated is not None
    assert updated["sync_frequency"] == "10m"
    # Persisted to config
    assert get_workspace(str(ws_dir))["sync_frequency"] == "10m"


def test_update_workspace_missing():
    """update_workspace returns None for unknown paths."""
    result = update_workspace("/not/there", sync_frequency="1h")
    assert result is None


# ---------------------------------------------------------------------------
# mark_workspace_synced
# ---------------------------------------------------------------------------


def test_mark_workspace_synced(tmp_path):
    """mark_workspace_synced sets last_synced to a non-None value."""
    ws_dir = tmp_path / "repo"
    ws_dir.mkdir()
    add_workspace(str(ws_dir))
    assert get_workspace(str(ws_dir))["last_synced"] is None

    mark_workspace_synced(str(ws_dir))
    last = get_workspace(str(ws_dir))["last_synced"]
    assert last is not None
    # ISO 8601 format: ends with Z
    assert last.endswith("Z")


def test_mark_workspace_synced_unknown_path():
    """mark_workspace_synced does nothing (no error) for an unknown path."""
    # Must not raise
    mark_workspace_synced("/not/registered")


# ---------------------------------------------------------------------------
# list_endpoints
# ---------------------------------------------------------------------------


def test_list_endpoints_empty():
    """list_endpoints returns [] when none are configured."""
    assert list_endpoints() == []


# ---------------------------------------------------------------------------
# add_endpoint
# ---------------------------------------------------------------------------


def test_add_endpoint_creates_entry():
    """add_endpoint registers a new remote endpoint."""
    entry = add_endpoint(
        endpoint_type="github",
        org="moia-oss",
        ssh_url_template="git@github.com:{org}/{repo}.git",
    )
    assert entry["org"] == "moia-oss"
    assert entry["type"] == "github"
    assert entry["auto_clone"] is True
    assert len(list_endpoints()) == 1


def test_add_endpoint_idempotent():
    """add_endpoint does not create duplicates for the same org."""
    add_endpoint("github", "moia-oss", "git@github.com:{org}/{repo}.git")
    add_endpoint("github", "moia-oss", "git@github.com:{org}/{repo}.git")
    assert len(list_endpoints()) == 1


def test_add_endpoint_custom_settings():
    """add_endpoint stores overridden settings."""
    entry = add_endpoint(
        "gitlab",
        "my-group",
        "git@gitlab.com:{org}/{repo}.git",
        auto_clone=False,
        discovery_frequency="2h",
        clone_base_path="/repos",
    )
    assert entry["auto_clone"] is False
    assert entry["discovery_frequency"] == "2h"
    assert entry["clone_base_path"] == "/repos"


# ---------------------------------------------------------------------------
# get_endpoint
# ---------------------------------------------------------------------------


def test_get_endpoint_existing():
    """get_endpoint returns the matching entry."""
    add_endpoint("github", "moia-oss", "git@github.com:{org}/{repo}.git")
    result = get_endpoint("moia-oss")
    assert result is not None
    assert result["org"] == "moia-oss"


def test_get_endpoint_missing():
    """get_endpoint returns None for unknown org."""
    assert get_endpoint("nonexistent-org") is None


# ---------------------------------------------------------------------------
# remove_endpoint
# ---------------------------------------------------------------------------


def test_remove_endpoint_existing():
    """remove_endpoint removes the entry and returns True."""
    add_endpoint("github", "moia-oss", "git@github.com:{org}/{repo}.git")
    result = remove_endpoint("moia-oss")
    assert result is True
    assert list_endpoints() == []


def test_remove_endpoint_missing():
    """remove_endpoint returns False when org is not registered."""
    result = remove_endpoint("not-there")
    assert result is False

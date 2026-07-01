"""Tests for cap init wizard and cap config commands."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
import pytest


class TestDetectAwsProfiles:
    """Tests for _detect_aws_profiles()."""

    def test_parses_profile_sections(self, tmp_path):
        """Should extract profile names from [profile xxx] sections."""
        aws_config = tmp_path / ".aws" / "config"
        aws_config.parent.mkdir(parents=True)
        aws_config.write_text(
            "[default]\nregion = us-east-1\n\n"
            "[profile dev-account]\nregion = eu-central-1\n\n"
            "[profile prod-account]\nregion = eu-west-1\n"
        )
        from cap.cli.lifecycle import _detect_aws_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_profiles()
        assert "default" in profiles
        assert "dev-account" in profiles
        assert "prod-account" in profiles

    def test_default_is_first(self, tmp_path):
        """default profile should be first in list."""
        aws_config = tmp_path / ".aws" / "config"
        aws_config.parent.mkdir(parents=True)
        aws_config.write_text(
            "[profile alpha]\nregion = us-east-1\n\n"
            "[default]\nregion = eu-central-1\n"
        )
        from cap.cli.lifecycle import _detect_aws_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_profiles()
        assert profiles[0] == "default"

    def test_empty_when_no_aws_config(self, tmp_path):
        """Should return empty list if ~/.aws/config doesn't exist."""
        from cap.cli.lifecycle import _detect_aws_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_profiles()
        assert profiles == []

    def test_no_profiles_section(self, tmp_path):
        """Should handle config with only default section."""
        aws_config = tmp_path / ".aws" / "config"
        aws_config.parent.mkdir(parents=True)
        aws_config.write_text("[default]\nregion = us-east-1\n")
        from cap.cli.lifecycle import _detect_aws_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_profiles()
        assert profiles == ["default"]


class TestModelTiers:
    """Tests for _MODEL_TIERS dictionary."""

    def test_economy_tier_exists(self):
        """economy tier should be present."""
        from cap.cli.lifecycle import _MODEL_TIERS
        assert "economy" in _MODEL_TIERS

    def test_economy_uses_haiku(self):
        """economy tier should use haiku for all agents."""
        from cap.cli.lifecycle import _MODEL_TIERS
        for agent, model in _MODEL_TIERS["economy"].items():
            assert model == "haiku", f"Expected haiku for {agent}, got {model}"

    def test_haiku_only_legacy_alias(self):
        """haiku-only should still exist as legacy alias."""
        from cap.cli.lifecycle import _MODEL_TIERS
        assert "haiku-only" in _MODEL_TIERS

    def test_balanced_tier_exists(self):
        """balanced tier should exist."""
        from cap.cli.lifecycle import _MODEL_TIERS
        assert "balanced" in _MODEL_TIERS

    def test_quality_tier_exists(self):
        """quality tier should exist."""
        from cap.cli.lifecycle import _MODEL_TIERS
        assert "quality" in _MODEL_TIERS


class TestConfigSetLogic:
    """Tests for cap config set logic (unit tests of the dict manipulation)."""

    def test_sets_nested_float(self):
        """Should coerce float values for budget settings."""
        data = {"budget": {"daily_limit_usd": 5.0}}
        parts = "budget.daily_limit_usd".split(".")
        target = data
        for part in parts[:-1]:
            target = target[part]
        old_value = target.get(parts[-1])
        assert isinstance(old_value, float)
        target[parts[-1]] = float("10")
        assert data["budget"]["daily_limit_usd"] == 10.0

    def test_creates_nested_path(self):
        """Should create intermediate dicts for new nested keys."""
        data = {"aws": {}}
        parts = "aws.profile".split(".")
        target = data
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = "my-new-profile"
        assert data["aws"]["profile"] == "my-new-profile"

    def test_sets_top_level_string(self):
        """Should set a top-level string value."""
        data = {"model_tier": "balanced"}
        parts = "model_tier".split(".")
        target = data
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = "quality"
        assert data["model_tier"] == "quality"


class TestSetupWizardNonInteractive:
    """Tests for non-interactive wizard mode."""

    def test_non_interactive_uses_first_profile(self, tmp_path):
        """Non-interactive should use first profile."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=["my-profile", "other"]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert result["aws"]["profile"] == "my-profile"

    def test_non_interactive_uses_eu_central_1(self, tmp_path):
        """Non-interactive should default to eu-central-1."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=["my-profile"]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert result["aws"]["region"] == "eu-central-1"

    def test_non_interactive_budget_default(self, tmp_path):
        """Non-interactive should default to $5 budget."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=["my-profile"]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert result["budget"]["daily_limit_usd"] == 5.0

    def test_non_interactive_skips_if_config_exists(self, tmp_path):
        """Non-interactive should skip if config already exists and force=False."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "harness-config.json"
        existing = {"aws": {"profile": "existing-profile"}}
        config_path.write_text(json.dumps(existing))

        with patch.object(lc, '_detect_aws_profiles', return_value=["new-profile"]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=False, non_interactive=True)

        # Should return existing config unchanged
        assert result["aws"]["profile"] == "existing-profile"

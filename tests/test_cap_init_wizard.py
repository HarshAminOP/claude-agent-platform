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


class TestDetectAwsCredentialProfiles:
    """Tests for _detect_aws_credential_profiles()."""

    def test_parses_credentials_file(self, tmp_path):
        """Should extract profile names from ~/.aws/credentials."""
        aws_creds = tmp_path / ".aws" / "credentials"
        aws_creds.parent.mkdir(parents=True)
        aws_creds.write_text(
            "[default]\naws_access_key_id = AKIA...\n\n"
            "[staging]\naws_access_key_id = AKIA...\n"
        )
        from cap.cli.lifecycle import _detect_aws_credential_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_credential_profiles()
        assert "default" in profiles
        assert "staging" in profiles

    def test_empty_when_no_credentials(self, tmp_path):
        """Should return empty list if ~/.aws/credentials doesn't exist."""
        from cap.cli.lifecycle import _detect_aws_credential_profiles
        with patch.object(Path, 'home', return_value=tmp_path):
            profiles = _detect_aws_credential_profiles()
        assert profiles == []


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

    def test_non_interactive_uses_env_vars_auth(self, tmp_path):
        """Non-interactive should default to env-vars auth method."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=["my-profile", "other"]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert result["provider"] == "aws-bedrock"
        assert result["aws"]["auth_method"] == "env-vars"

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

    def test_non_interactive_has_provider_field(self, tmp_path):
        """Non-interactive config should include provider field."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=[]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert result["provider"] == "aws-bedrock"

    def test_non_interactive_has_models(self, tmp_path):
        """Non-interactive config should include model IDs for aws-bedrock."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=[]):
            with patch.object(Path, 'home', return_value=tmp_path):
                result = lc._run_setup_wizard(force=True, non_interactive=True)

        assert "models" in result
        assert "haiku" in result["models"]
        assert "sonnet" in result["models"]
        assert "opus" in result["models"]

    def test_non_interactive_writes_config_file(self, tmp_path):
        """Non-interactive should write config to disk."""
        import cap.cli.lifecycle as lc
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)

        with patch.object(lc, '_detect_aws_profiles', return_value=[]):
            with patch.object(Path, 'home', return_value=tmp_path):
                lc._run_setup_wizard(force=True, non_interactive=True)

        config_path = config_dir / "harness-config.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data["provider"] == "aws-bedrock"


class TestProviderConstants:
    """Tests for provider and auth method constants."""

    def test_provider_constants_defined(self):
        """All provider constants should be defined."""
        from cap.cli.lifecycle import (
            PROVIDER_AWS_BEDROCK,
            PROVIDER_ANTHROPIC_API,
            PROVIDER_AZURE_OPENAI,
            PROVIDER_LOCAL,
        )
        assert PROVIDER_AWS_BEDROCK == "aws-bedrock"
        assert PROVIDER_ANTHROPIC_API == "anthropic-api"
        assert PROVIDER_AZURE_OPENAI == "azure-openai"
        assert PROVIDER_LOCAL == "local"

    def test_auth_method_constants_defined(self):
        """All auth method constants should be defined."""
        from cap.cli.lifecycle import (
            AUTH_SSO_PROFILE,
            AUTH_ENV_VARS,
            AUTH_STATIC_CREDENTIALS,
            AUTH_INSTANCE_ROLE,
        )
        assert AUTH_SSO_PROFILE == "sso-profile"
        assert AUTH_ENV_VARS == "env-vars"
        assert AUTH_STATIC_CREDENTIALS == "static-credentials"
        assert AUTH_INSTANCE_ROLE == "instance-role"

    def test_provider_model_defaults_all_providers(self):
        """Each provider should have model defaults."""
        from cap.cli.lifecycle import _PROVIDER_MODEL_DEFAULTS
        assert "aws-bedrock" in _PROVIDER_MODEL_DEFAULTS
        assert "anthropic-api" in _PROVIDER_MODEL_DEFAULTS
        assert "azure-openai" in _PROVIDER_MODEL_DEFAULTS
        assert "local" in _PROVIDER_MODEL_DEFAULTS

    def test_provider_model_defaults_have_all_tiers(self):
        """Each provider's model defaults should have haiku, sonnet, opus."""
        from cap.cli.lifecycle import _PROVIDER_MODEL_DEFAULTS
        for provider, models in _PROVIDER_MODEL_DEFAULTS.items():
            assert "haiku" in models, f"{provider} missing haiku"
            assert "sonnet" in models, f"{provider} missing sonnet"
            assert "opus" in models, f"{provider} missing opus"


class TestHarnessConfigBackwardCompat:
    """Tests for backward compatibility of harness_config loading."""

    def test_load_config_adds_provider_if_missing(self, tmp_path):
        """Old configs without provider field should default to aws-bedrock."""
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "harness-config.json"
        # Old-style config without provider
        old_config = {
            "aws": {"profile": "my-profile", "region": "eu-central-1"},
            "models": {"haiku": "model-id"},
        }
        config_path.write_text(json.dumps(old_config))

        from cap.lib.harness_config import load_harness_config
        with patch.object(Path, 'home', return_value=tmp_path):
            result = load_harness_config()

        assert result["provider"] == "aws-bedrock"

    def test_load_config_adds_auth_method_if_missing(self, tmp_path):
        """Old configs without auth_method should default to sso-profile."""
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "harness-config.json"
        old_config = {
            "aws": {"profile": "my-profile", "region": "eu-central-1"},
        }
        config_path.write_text(json.dumps(old_config))

        from cap.lib.harness_config import load_harness_config
        with patch.object(Path, 'home', return_value=tmp_path):
            result = load_harness_config()

        assert result["aws"]["auth_method"] == "sso-profile"

    def test_load_config_preserves_existing_provider(self, tmp_path):
        """Configs with provider field should keep their value."""
        config_dir = tmp_path / ".claude-platform"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "harness-config.json"
        config = {
            "provider": "anthropic-api",
            "anthropic": {"api_key_env": "MY_KEY"},
            "aws": {"profile": "", "region": "us-east-1", "auth_method": "env-vars"},
        }
        config_path.write_text(json.dumps(config))

        from cap.lib.harness_config import load_harness_config
        with patch.object(Path, 'home', return_value=tmp_path):
            result = load_harness_config()

        assert result["provider"] == "anthropic-api"

    def test_get_provider_helper(self, tmp_path):
        """get_provider should return the provider from config."""
        from cap.lib.harness_config import get_provider
        config = {"provider": "local"}
        assert get_provider(config) == "local"

    def test_get_provider_defaults_to_bedrock(self):
        """get_provider without config should default to aws-bedrock."""
        from cap.lib.harness_config import get_provider
        assert get_provider({}) == "aws-bedrock"

    def test_get_anthropic_api_key_from_env(self):
        """get_anthropic_api_key should read from the configured env var."""
        from cap.lib.harness_config import get_anthropic_api_key
        config = {"anthropic": {"api_key_env": "TEST_ANTHROPIC_KEY"}}
        with patch.dict(os.environ, {"TEST_ANTHROPIC_KEY": "sk-test-123"}):
            key = get_anthropic_api_key(config)
        assert key == "sk-test-123"

    def test_get_anthropic_api_key_returns_none_when_unset(self):
        """get_anthropic_api_key should return None if env var not set."""
        from cap.lib.harness_config import get_anthropic_api_key
        config = {"anthropic": {"api_key_env": "NONEXISTENT_VAR_XYZ"}}
        with patch.dict(os.environ, {}, clear=False):
            # Ensure the var doesn't exist
            os.environ.pop("NONEXISTENT_VAR_XYZ", None)
            key = get_anthropic_api_key(config)
        assert key is None


class TestProviderFactory:
    """Tests for _create_provider_client factory."""

    def test_aws_bedrock_returns_none(self):
        """aws-bedrock provider should return None (boto3 managed internally)."""
        from cap.harness.converse_executor import _create_provider_client
        config = {"provider": "aws-bedrock", "aws": {"profile": "", "region": "us-east-1"}}
        result = _create_provider_client(config)
        assert result is None

    def test_azure_raises_not_implemented(self):
        """azure-openai should raise NotImplementedError."""
        from cap.harness.converse_executor import _create_provider_client
        config = {"provider": "azure-openai", "azure": {"endpoint": ""}}
        with pytest.raises(NotImplementedError, match="Azure provider coming soon"):
            _create_provider_client(config)

    def test_local_raises_not_implemented(self):
        """local provider should raise NotImplementedError."""
        from cap.harness.converse_executor import _create_provider_client
        config = {"provider": "local", "local": {"base_url": "http://localhost:11434"}}
        with pytest.raises(NotImplementedError, match="Local provider coming soon"):
            _create_provider_client(config)

    def test_unknown_provider_raises_value_error(self):
        """Unknown provider should raise ValueError."""
        from cap.harness.converse_executor import _create_provider_client
        config = {"provider": "unknown-thing"}
        with pytest.raises(ValueError, match="Unknown provider"):
            _create_provider_client(config)

    def test_anthropic_raises_when_no_key(self):
        """anthropic-api should raise ValueError when env var is not set."""
        from cap.harness.converse_executor import _create_provider_client
        config = {"provider": "anthropic-api", "anthropic": {"api_key_env": "NONEXISTENT_CAP_TEST_KEY"}}
        os.environ.pop("NONEXISTENT_CAP_TEST_KEY", None)
        with pytest.raises(ValueError, match="API key not found"):
            _create_provider_client(config)


class TestConverseExecutorProviderClient:
    """Tests for ConverseExecutor with provider_client parameter."""

    def test_executor_accepts_provider_client(self):
        """ConverseExecutor should accept a provider_client kwarg."""
        from cap.harness.converse_executor import ConverseExecutor
        mock_provider = MagicMock()
        executor = ConverseExecutor(provider_client=mock_provider)
        assert executor._provider_client is mock_provider

    def test_ensure_client_skips_boto3_when_provider_set(self):
        """_ensure_client should skip boto3 init when provider_client is set."""
        from cap.harness.converse_executor import ConverseExecutor
        mock_provider = MagicMock()
        executor = ConverseExecutor(provider_client=mock_provider)
        executor._ensure_client()
        # Should not create boto3 client
        assert executor._client is None
        assert executor._available is True

    def test_get_client_returns_provider_when_set(self):
        """_get_client should return provider_client when available."""
        from cap.harness.converse_executor import ConverseExecutor
        mock_provider = MagicMock()
        executor = ConverseExecutor(provider_client=mock_provider)
        assert executor._get_client() is mock_provider

    def test_call_converse_routes_to_provider(self):
        """_call_converse should route to provider_client.converse when set."""
        from cap.harness.converse_executor import ConverseExecutor
        mock_provider = MagicMock()
        mock_provider.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        executor = ConverseExecutor(provider_client=mock_provider)
        executor._ensure_client()

        result = executor._call_converse(
            model_id="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
        )

        mock_provider.converse.assert_called_once()
        assert result["stopReason"] == "end_turn"

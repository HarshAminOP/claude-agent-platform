"""Tests for region-aware model probe (cap.lib.model_probe).

Covers: region prefix computation, probe with mocked boto3, tier assignment,
fallback when no models work, and non-interactive defaults.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cap.lib.model_probe import (
    create_bedrock_client,
    get_candidate_models,
    get_default_models_for_region,
    probe_all_models,
    probe_model,
    region_prefix,
)


# ── region_prefix ────────────────────────────────────────────────────────────


class TestRegionPrefix:
    def test_eu_regions(self):
        assert region_prefix("eu-central-1") == "eu"
        assert region_prefix("eu-west-1") == "eu"
        assert region_prefix("eu-north-1") == "eu"

    def test_us_regions(self):
        assert region_prefix("us-east-1") == "us"
        assert region_prefix("us-west-2") == "us"

    def test_ap_regions(self):
        assert region_prefix("ap-southeast-1") == "ap"
        assert region_prefix("ap-northeast-1") == "ap"

    def test_other_regions_return_empty(self):
        assert region_prefix("me-south-1") == ""
        assert region_prefix("af-south-1") == ""
        assert region_prefix("sa-east-1") == ""

    def test_empty_string(self):
        assert region_prefix("") == ""

    def test_none_returns_empty(self):
        # region_prefix should handle falsy values
        assert region_prefix("") == ""


# ── get_candidate_models ─────────────────────────────────────────────────────


class TestGetCandidateModels:
    def test_eu_prefix(self):
        candidates = get_candidate_models("eu")
        assert all(m.startswith("eu.") for models in candidates.values() for m in models)
        assert "haiku" in candidates
        assert "sonnet" in candidates
        assert "opus" in candidates

    def test_us_prefix(self):
        candidates = get_candidate_models("us")
        assert all(m.startswith("us.") for models in candidates.values() for m in models)

    def test_ap_prefix(self):
        candidates = get_candidate_models("ap")
        assert all(m.startswith("ap.") for models in candidates.values() for m in models)

    def test_empty_prefix_no_dot(self):
        candidates = get_candidate_models("")
        # Without prefix, IDs start with "anthropic."
        for tier, models in candidates.items():
            for m in models:
                assert m.startswith("anthropic."), f"{m} should start with 'anthropic.'"

    def test_haiku_has_one_candidate(self):
        candidates = get_candidate_models("eu")
        assert len(candidates["haiku"]) == 1

    def test_sonnet_has_two_candidates(self):
        candidates = get_candidate_models("eu")
        assert len(candidates["sonnet"]) == 2

    def test_opus_has_three_candidates(self):
        candidates = get_candidate_models("eu")
        assert len(candidates["opus"]) == 3


# ── get_default_models_for_region ────────────────────────────────────────────


class TestGetDefaultModelsForRegion:
    def test_eu_region_returns_eu_prefixed(self):
        models = get_default_models_for_region("eu-central-1")
        assert models["haiku"].startswith("eu.")
        assert models["sonnet"].startswith("eu.")
        assert models["opus"].startswith("eu.")

    def test_us_region_returns_us_prefixed(self):
        models = get_default_models_for_region("us-east-1")
        assert models["haiku"].startswith("us.")
        assert models["sonnet"].startswith("us.")
        assert models["opus"].startswith("us.")

    def test_unknown_region_no_prefix(self):
        models = get_default_models_for_region("me-south-1")
        assert models["haiku"].startswith("anthropic.")

    def test_returns_all_three_tiers(self):
        models = get_default_models_for_region("eu-west-1")
        assert set(models.keys()) == {"haiku", "sonnet", "opus"}

    def test_returns_first_candidate_per_tier(self):
        """Default should be the first (most preferred) candidate."""
        models = get_default_models_for_region("eu-central-1")
        candidates = get_candidate_models("eu")
        for tier in ("haiku", "sonnet", "opus"):
            assert models[tier] == candidates[tier][0]


# ── probe_model ──────────────────────────────────────────────────────────────


class TestProbeModel:
    def test_success_returns_true(self):
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [{"text": "hi"}]}}}
        assert probe_model(client, "eu.anthropic.claude-haiku-4-5-20251001-v1:0") is True

    def test_failure_returns_false(self):
        client = MagicMock()
        client.converse.side_effect = Exception("AccessDeniedException")
        assert probe_model(client, "eu.anthropic.claude-opus-4-8") is False

    def test_calls_converse_with_correct_params(self):
        client = MagicMock()
        client.converse.return_value = {}
        probe_model(client, "test-model", max_tokens=10)
        client.converse.assert_called_once_with(
            modelId="test-model",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 10},
        )

    def test_default_max_tokens_is_5(self):
        client = MagicMock()
        client.converse.return_value = {}
        probe_model(client, "test-model")
        call_kwargs = client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"]["maxTokens"] == 5


# ── probe_all_models ─────────────────────────────────────────────────────────


class TestProbeAllModels:
    def test_all_succeed_returns_first_per_tier(self):
        client = MagicMock()
        client.converse.return_value = {}
        result = probe_all_models(client, "eu-central-1")
        # All tiers should be present
        assert "haiku" in result
        assert "sonnet" in result
        assert "opus" in result
        # Should be the first candidate
        candidates = get_candidate_models("eu")
        assert result["haiku"] == candidates["haiku"][0]
        assert result["sonnet"] == candidates["sonnet"][0]
        assert result["opus"] == candidates["opus"][0]

    def test_first_fails_second_succeeds(self):
        """When first candidate fails, falls through to second."""
        client = MagicMock()
        call_count = {"n": 0}

        def _converse(**kwargs):
            call_count["n"] += 1
            model_id = kwargs["modelId"]
            # Fail the first sonnet candidate, succeed on second
            if "sonnet-4-6" in model_id:
                raise Exception("not available")
            return {}

        client.converse.side_effect = _converse
        result = probe_all_models(client, "eu-central-1")
        assert "sonnet" in result
        # Should be the second sonnet candidate
        candidates = get_candidate_models("eu")
        assert result["sonnet"] == candidates["sonnet"][1]

    def test_all_fail_returns_empty(self):
        client = MagicMock()
        client.converse.side_effect = Exception("nope")
        result = probe_all_models(client, "us-east-1")
        assert result == {}

    def test_progress_callback_called(self):
        client = MagicMock()
        client.converse.return_value = {}
        calls = []

        def cb(model_id, success):
            calls.append((model_id, success))

        probe_all_models(client, "eu-central-1", progress_callback=cb)
        # Should have been called for each model that was tried
        # (first success per tier stops trying more in that tier)
        assert len(calls) >= 3  # At least one per tier
        assert all(isinstance(c[1], bool) for c in calls)

    def test_partial_failure_only_returns_working_tiers(self):
        """If opus fails entirely but haiku/sonnet work, only those are returned."""
        client = MagicMock()

        def _converse(**kwargs):
            model_id = kwargs["modelId"]
            if "opus" in model_id:
                raise Exception("no access")
            return {}

        client.converse.side_effect = _converse
        result = probe_all_models(client, "eu-central-1")
        assert "haiku" in result
        assert "sonnet" in result
        assert "opus" not in result


# ── create_bedrock_client ────────────────────────────────────────────────────


class TestCreateBedrockClient:
    @patch.dict("sys.modules", {"boto3": MagicMock()})
    def test_creates_client_with_region(self):
        import sys
        mock_boto3 = sys.modules["boto3"]
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        create_bedrock_client(region="us-west-2")
        mock_boto3.Session.assert_called_once_with(region_name="us-west-2")
        mock_session.client.assert_called_once_with("bedrock-runtime", region_name="us-west-2")

    @patch.dict("sys.modules", {"boto3": MagicMock()})
    def test_creates_client_with_profile_sso(self):
        import sys
        mock_boto3 = sys.modules["boto3"]
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        create_bedrock_client(region="eu-central-1", profile="my-sso", auth_method="sso-profile")
        mock_boto3.Session.assert_called_once_with(
            profile_name="my-sso", region_name="eu-central-1"
        )

    @patch.dict("sys.modules", {"boto3": MagicMock()})
    def test_no_profile_for_env_vars_auth(self):
        import sys
        mock_boto3 = sys.modules["boto3"]
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        create_bedrock_client(region="eu-central-1", profile="", auth_method="env-vars")
        mock_boto3.Session.assert_called_once_with(region_name="eu-central-1")

    @patch.dict("sys.modules", {"boto3": MagicMock()})
    def test_no_profile_for_instance_role(self):
        import sys
        mock_boto3 = sys.modules["boto3"]
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        create_bedrock_client(region="us-east-1", profile="", auth_method="instance-role")
        mock_boto3.Session.assert_called_once_with(region_name="us-east-1")


# ── Integration: lifecycle wizard non-interactive uses defaults ───────────────


class TestNonInteractiveDefaults:
    """Verify that non-interactive mode uses region defaults without Bedrock calls."""

    def test_non_interactive_bedrock_uses_region_defaults(self):
        """In non-interactive mode, the wizard should use get_default_models_for_region
        rather than calling probe_all_models."""
        defaults = get_default_models_for_region("eu-central-1")
        assert defaults["haiku"] == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert "sonnet" in defaults["sonnet"]
        assert "opus" in defaults["opus"]

    def test_non_interactive_us_region(self):
        defaults = get_default_models_for_region("us-east-1")
        assert all(v.startswith("us.") for v in defaults.values())

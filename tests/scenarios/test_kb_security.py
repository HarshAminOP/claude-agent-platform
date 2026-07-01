"""Scenario tests for KB secret-scanning reliability.

Covers cap.lib.secret_scanner: pattern detection, entropy detection,
the reject_if_secrets gate function, and the SKIP_FILE_PATTERNS registry.

Each test is independent and completes well under 100 ms — no I/O required.
"""
from __future__ import annotations

import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cap.lib.secret_scanner import (
    SKIP_FILE_PATTERNS,
    SecretDetected,
    _shannon_entropy,
    reject_if_secrets,
    scan_for_secrets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _high_entropy_string(length: int = 32, seed: int = 99) -> str:
    """Return a random alphanumeric string with high Shannon entropy.

    length=32 with seed=99 produces entropy ~4.56 bits, safely above the
    4.5 threshold used by secret_scanner._ENTROPY_THRESHOLD.
    """
    rng = random.Random(seed)
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choices(alphabet, k=length))


# ---------------------------------------------------------------------------
# Individual pattern detection via scan_for_secrets
# ---------------------------------------------------------------------------


class TestAwsAccessKeyDetected:
    def test_aws_access_key_detected(self) -> None:
        content = "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        findings = scan_for_secrets(content)
        assert "aws_access_key" in findings

    def test_aws_access_key_in_config_block(self) -> None:
        content = "[default]\naws_access_key_id = AKIAIOSFODNN7EXAMPLEFOO\n"
        # The regex matches 16 upper-alnum chars after AKIA
        findings = scan_for_secrets(content)
        assert "aws_access_key" in findings

    def test_no_false_positive_on_partial_key(self) -> None:
        # Only 10 chars after AKIA — too short to match the 16-char pattern.
        content = "AKIA1234567890"
        findings = scan_for_secrets(content)
        assert "aws_access_key" not in findings


class TestPrivateKeyDetected:
    def test_private_key_detected(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        findings = scan_for_secrets(content)
        assert "private_key" in findings

    def test_ec_private_key_detected(self) -> None:
        content = "-----BEGIN EC PRIVATE KEY-----\nbase64data\n-----END EC PRIVATE KEY-----"
        findings = scan_for_secrets(content)
        assert "private_key" in findings

    def test_openssh_private_key_detected(self) -> None:
        content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC...\n-----END OPENSSH PRIVATE KEY-----"
        findings = scan_for_secrets(content)
        assert "private_key" in findings


class TestGithubPatDetected:
    def test_github_pat_detected(self) -> None:
        # 36-char suffix after ghp_
        pat = "ghp_" + "a" * 36
        content = f"token: {pat}"
        findings = scan_for_secrets(content)
        assert "github_pat" in findings

    def test_github_pat_in_env_line(self) -> None:
        # The regex requires exactly 36 alphanumeric chars after "ghp_" (total 40 chars).
        pat = "ghp_" + "A" * 36
        content = f"GITHUB_TOKEN={pat}\n"
        findings = scan_for_secrets(content)
        assert "github_pat" in findings

    def test_short_prefix_not_matched(self) -> None:
        # ghp_ but only 10 chars — must not match
        content = "ghp_shorttoken"
        findings = scan_for_secrets(content)
        assert "github_pat" not in findings


class TestSlackTokenDetected:
    def test_slack_token_detected(self) -> None:
        content = "SLACK_TOKEN=xoxb-something-here-12345"
        findings = scan_for_secrets(content)
        assert "slack_token" in findings

    def test_slack_bot_token(self) -> None:
        content = "token = xoxb-fake0test0val"
        findings = scan_for_secrets(content)
        assert "slack_token" in findings

    def test_slack_user_token(self) -> None:
        content = "xoxp-12345-67890-abcdef-ghijkl"
        findings = scan_for_secrets(content)
        assert "slack_token" in findings

    def test_non_slack_xox_not_matched(self) -> None:
        # 'xox' but missing the required pattern letter and dash
        content = "xoxnotaslacktoken"
        findings = scan_for_secrets(content)
        assert "slack_token" not in findings


class TestHighEntropyStringDetected:
    def test_high_entropy_string_detected(self) -> None:
        # 32-char random alphanumeric (seed=99) has entropy ~4.56 bits,
        # reliably above the 4.5 threshold used by the scanner.
        candidate = _high_entropy_string()
        entropy = _shannon_entropy(candidate)
        assert entropy > 4.5, f"Fixture has insufficient entropy: {entropy}"
        content = f"api_key = {candidate}"
        findings = scan_for_secrets(content)
        assert "generic_high_entropy" in findings

    def test_low_entropy_string_not_flagged(self) -> None:
        # Repeated character — Shannon entropy ~ 0
        content = "password = " + "a" * 30
        findings = scan_for_secrets(content)
        assert "generic_high_entropy" not in findings

    def test_short_string_below_min_length_not_flagged(self) -> None:
        # The high-entropy regex requires 20+ chars; 10-char string is ignored.
        content = "key = Ab1Cd2Ef3G"  # 15 chars alphanumeric
        findings = scan_for_secrets(content)
        assert "generic_high_entropy" not in findings

    def test_entropy_threshold_boundary(self) -> None:
        # A string of exactly two alternating chars has entropy ~ 1.0 — well below.
        content = "ab" * 15  # 30 chars, 2 unique chars → entropy = 1.0
        findings = scan_for_secrets(content)
        assert "generic_high_entropy" not in findings


class TestCleanCodePasses:
    def test_clean_python_code_passes(self) -> None:
        content = '''
def fetch_user(user_id: int) -> dict:
    """Retrieve user record from the database."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else {}
'''
        findings = scan_for_secrets(content)
        assert findings == [], f"Unexpected findings in clean code: {findings}"

    def test_clean_yaml_config_passes(self) -> None:
        content = """
app:
  name: my-service
  replicas: 3
  port: 8080
  log_level: info
"""
        findings = scan_for_secrets(content)
        assert findings == [], f"Unexpected findings in clean YAML: {findings}"

    def test_clean_terraform_passes(self) -> None:
        content = """
resource "aws_s3_bucket" "data" {
  bucket = "my-app-data-bucket"
  tags = {
    Environment = "production"
    Team        = "platform"
  }
}
"""
        findings = scan_for_secrets(content)
        assert findings == [], f"Unexpected findings in Terraform: {findings}"


# ---------------------------------------------------------------------------
# reject_if_secrets gate
# ---------------------------------------------------------------------------


class TestRejectIfSecretsRaises:
    def test_reject_if_secrets_raises_on_aws_key(self) -> None:
        content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        with pytest.raises(SecretDetected) as exc_info:
            reject_if_secrets(content, source_path="config.env")
        assert exc_info.value.pattern_name == "aws_access_key"

    def test_reject_if_secrets_raises_on_private_key(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\ndata\n-----END RSA PRIVATE KEY-----"
        with pytest.raises(SecretDetected) as exc_info:
            reject_if_secrets(content, source_path="key.pem")
        assert exc_info.value.pattern_name == "private_key"

    def test_secret_detected_exception_has_pattern_name(self) -> None:
        content = "ghp_" + "Z" * 36
        with pytest.raises(SecretDetected) as exc_info:
            reject_if_secrets(content, source_path=".env")
        exc = exc_info.value
        assert exc.pattern_name == "github_pat"
        assert isinstance(exc.redacted_match, str)
        assert len(exc.redacted_match) > 0

    def test_secret_detected_str_includes_pattern_name(self) -> None:
        content = "xoxb-fake0test0val"
        with pytest.raises(SecretDetected) as exc_info:
            reject_if_secrets(content, source_path="app.py")
        assert "slack_token" in str(exc_info.value)


class TestRejectIfSecretsPassesClean:
    def test_reject_if_secrets_passes_clean_content(self) -> None:
        content = "def hello():\n    print('hello world')\n"
        result = reject_if_secrets(content, source_path="hello.py")
        assert result == content

    def test_returns_content_unchanged(self) -> None:
        original = "name: my-service\nversion: 1.0.0\n"
        returned = reject_if_secrets(original, source_path="pyproject.toml")
        assert returned is original or returned == original

    def test_empty_content_passes(self) -> None:
        result = reject_if_secrets("", source_path="empty.py")
        assert result == ""


# ---------------------------------------------------------------------------
# SKIP_FILE_PATTERNS registry
# ---------------------------------------------------------------------------


class TestSkipFilePatterns:
    def test_pem_in_skip_patterns(self) -> None:
        assert "*.pem" in SKIP_FILE_PATTERNS

    def test_key_in_skip_patterns(self) -> None:
        assert "*.key" in SKIP_FILE_PATTERNS

    def test_env_in_skip_patterns(self) -> None:
        assert ".env" in SKIP_FILE_PATTERNS

    def test_tfstate_in_skip_patterns(self) -> None:
        assert "terraform.tfstate" in SKIP_FILE_PATTERNS

    def test_skip_patterns_is_frozenset(self) -> None:
        assert isinstance(SKIP_FILE_PATTERNS, frozenset)

    def test_secrets_yaml_in_skip_patterns(self) -> None:
        assert "secrets.yaml" in SKIP_FILE_PATTERNS

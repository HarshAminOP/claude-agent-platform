"""Secret detection gate for knowledge base ingestion.

Scans content for credentials, keys, tokens, and high-entropy strings
before they enter the knowledge base. Raises SecretDetected on match.
"""

from __future__ import annotations

import math
import re
from typing import FrozenSet


class SecretDetected(Exception):
    """Raised when a secret pattern is detected in content destined for ingestion."""

    def __init__(self, pattern_name: str, redacted_match: str) -> None:
        self.pattern_name = pattern_name
        self.redacted_match = redacted_match
        super().__init__(
            f"Secret detected [{pattern_name}]: {redacted_match}"
        )


# Compiled regex patterns for secret detection.
# Each pattern targets a specific credential format.
PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key": re.compile(
        r"AKIA[0-9A-Z]{16}"
    ),
    "aws_secret_key": re.compile(
        r"(?:secret|aws).{0,50}([A-Za-z0-9/+=]{40})",
        re.IGNORECASE,
    ),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
    "github_pat": re.compile(
        r"(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})"
    ),
    "slack_token": re.compile(
        r"xox[baprs]-[A-Za-z0-9\-]{10,}"
    ),
}


# File patterns that should never be ingested during sync.
SKIP_FILE_PATTERNS: FrozenSet[str] = frozenset({
    "credentials",
    "*.pem",
    "*.key",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "secrets.yaml",
    "secrets.yml",
    ".env",
    ".env.*",
})


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string in bits.

    Higher entropy indicates more randomness, typical of secrets/keys.
    """
    if not s:
        return 0.0

    length = len(s)
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1

    entropy = 0.0
    for count in freq.values():
        probability = count / length
        if probability > 0:
            entropy -= probability * math.log2(probability)

    return entropy


# Pattern for generic high-entropy detection: 20+ alphanumeric characters.
_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9/+=\-_]{20,}")

# Minimum Shannon entropy threshold to flag as a potential secret.
_ENTROPY_THRESHOLD = 4.5


def scan_for_secrets(content: str) -> list[str]:
    """Scan content for secret patterns.

    Returns a list of pattern names that matched. An empty list means
    the content is clean.
    """
    findings: list[str] = []

    for pattern_name, pattern in PATTERNS.items():
        if pattern.search(content):
            findings.append(pattern_name)

    # Generic high-entropy string detection.
    # Only flag if no other patterns already matched for the same region,
    # but we check independently to catch novel secret formats.
    if "generic_high_entropy" not in findings:
        for match in _HIGH_ENTROPY_RE.finditer(content):
            candidate = match.group(0)
            if _shannon_entropy(candidate) > _ENTROPY_THRESHOLD:
                findings.append("generic_high_entropy")
                break

    return findings


def _redact(match_text: str) -> str:
    """Redact a matched secret, preserving first 4 and last 4 chars."""
    if len(match_text) <= 12:
        return match_text[:2] + "***" + match_text[-2:]
    return match_text[:4] + "***" + match_text[-4:]


def reject_if_secrets(content: str, source_path: str) -> str:
    """Gate function: reject content containing secrets.

    Args:
        content: The text content to scan.
        source_path: Path to the source file (for error context).

    Returns:
        The content unchanged if no secrets are found.

    Raises:
        SecretDetected: If any secret pattern matches.
    """
    findings = scan_for_secrets(content)

    if findings:
        # Find the actual match text for the first finding to produce
        # a useful redacted example in the exception.
        pattern_name = findings[0]
        redacted = "[content from " + source_path + "]"

        if pattern_name == "generic_high_entropy":
            for match in _HIGH_ENTROPY_RE.finditer(content):
                candidate = match.group(0)
                if _shannon_entropy(candidate) > _ENTROPY_THRESHOLD:
                    redacted = _redact(candidate)
                    break
        elif pattern_name in PATTERNS:
            match = PATTERNS[pattern_name].search(content)
            if match:
                matched_text = match.group(1) if match.lastindex else match.group(0)
                redacted = _redact(matched_text)

        raise SecretDetected(
            pattern_name=pattern_name,
            redacted_match=redacted,
        )

    return content

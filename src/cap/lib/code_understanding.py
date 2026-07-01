"""LLM-powered semantic analysis of code repositories and modules.

Uses AWS Bedrock to generate structured understanding of codebases —
architectural patterns, domain classification, service interactions, and
embeddings-optimised text for semantic search.

Haiku is used for single-repo/module analysis (cost-efficient). Sonnet is
used for cross-service interaction analysis (higher reasoning required).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("cap.code_understanding")

# Error codes that indicate a non-retryable permanent Bedrock failure.
_PERMANENT_ERROR_CODES = frozenset({
    "ModelNotReadyException",
    "ServiceUnavailableException",
    "AccessDeniedException",
    "ValidationException",
})

# Error codes that warrant exponential back-off and a retry.
_RETRYABLE_ERROR_CODES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
})


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class BudgetExceeded(Exception):
    """Raised when the per-run budget limit is exceeded."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class UnderstandingConfig:
    """Configuration for the LLM-powered code understanding module.

    Attributes:
        analysis_model: Bedrock model ID used for single-repo/module analysis.
            Defaults to Haiku for cost efficiency.
        complex_model: Bedrock model ID used for complex cross-service analysis.
            Defaults to Sonnet for higher reasoning quality.
        max_input_chars: Maximum characters passed in a single LLM prompt.
            Longer inputs are truncated before the call.
        max_concurrent: Maximum simultaneous in-flight Bedrock calls.
        region: AWS region for the Bedrock Runtime endpoint.
        profile: Optional AWS named profile; None uses the ambient credential
            chain (instance role, env vars, etc.).
        cost_per_1k_input_haiku: USD cost per 1 000 input tokens for Haiku.
        cost_per_1k_output_haiku: USD cost per 1 000 output tokens for Haiku.
        cost_per_1k_input_sonnet: USD cost per 1 000 input tokens for Sonnet.
        cost_per_1k_output_sonnet: USD cost per 1 000 output tokens for Sonnet.
        max_retries: Maximum attempts per call before raising or returning a
            degraded result.
        budget_limit_usd: Maximum USD to spend in a single indexing run.
            BudgetExceeded is raised if this limit is crossed.
    """

    analysis_model: str = ""  # resolved at construction via UnderstandingConfig.default()
    complex_model: str = ""   # resolved at construction via UnderstandingConfig.default()
    max_input_chars: int = 50_000
    max_concurrent: int = 3
    region: str = ""          # resolved at construction via UnderstandingConfig.default()
    profile: Optional[str] = None
    cost_per_1k_input_haiku: float = 0.001
    cost_per_1k_output_haiku: float = 0.005
    cost_per_1k_input_sonnet: float = 0.003
    cost_per_1k_output_sonnet: float = 0.015
    max_retries: int = 3
    budget_limit_usd: float = 2.0

    def __post_init__(self) -> None:
        """Resolve empty fields from harness config so callers need not pass models/region."""
        if not self.analysis_model or not self.complex_model or not self.region:
            try:
                from cap.lib.harness_config import load_harness_config
                cfg = load_harness_config()
                models = cfg.get("models", {})
                embeddings = cfg.get("embeddings", {})
                aws = cfg.get("aws", {})
                if not self.region:
                    self.region = (
                        embeddings.get("region")
                        or aws.get("region")
                        or "us-east-1"
                    )
                if self.profile is None:
                    self.profile = aws.get("profile") or None
                # Apply region prefix to model IDs
                prefix = self._region_prefix(self.region)
                if not self.analysis_model:
                    raw = models.get("haiku") or "anthropic.claude-haiku-4-5-20251001-v1:0"
                    self.analysis_model = self._apply_prefix(raw, prefix)
                if not self.complex_model:
                    raw = models.get("sonnet") or "anthropic.claude-sonnet-4-6-20250929-v1:0"
                    self.complex_model = self._apply_prefix(raw, prefix)
            except Exception:
                if not self.region:
                    self.region = "us-east-1"
                prefix = self._region_prefix(self.region)
                if not self.analysis_model:
                    self.analysis_model = f"{prefix}.anthropic.claude-haiku-4-5-20251001-v1:0"
                if not self.complex_model:
                    self.complex_model = f"{prefix}.anthropic.claude-sonnet-4-6-20250929-v1:0"

    @staticmethod
    def _region_prefix(region: str) -> str:
        """Map AWS region to Bedrock model ID prefix."""
        if region.startswith("eu"):
            return "eu"
        if region.startswith("ap"):
            return "ap"
        return "us"

    @staticmethod
    def _apply_prefix(model_id: str, prefix: str) -> str:
        """Strip existing region prefix and apply the correct one."""
        for p in ("us.", "eu.", "ap."):
            if model_id.startswith(p):
                model_id = model_id[len(p):]
                break
        return f"{prefix}.{model_id}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RepoUnderstanding:
    """Semantic understanding of a single code repository.

    Attributes:
        name: Repository name.
        summary: 2-3 sentence architectural description.
        architectural_pattern: Primary pattern — event-driven, request-response,
            pub-sub, batch, cron, library, or CLI.
        domain: Classification — infrastructure, platform, application, tooling,
            observability, data, or security.
        complexity: Estimated complexity rating on a 1–5 scale.
        exposes: APIs, events, or resources this repo publishes/exposes.
        consumes: External services or APIs this repo depends on.
        tags: Semantic tags suitable for search and filtering.
        confidence: LLM confidence in the analysis (0.0–1.0).
        model_used: Bedrock model ID that generated this understanding.
        cost_usd: Estimated USD cost of the analysis call.
    """

    name: str
    summary: str
    architectural_pattern: str
    domain: str
    complexity: int
    exposes: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    model_used: str = ""
    cost_usd: float = 0.0


@dataclass
class ModuleUnderstanding:
    """Semantic understanding of a reusable module (Terraform, Helm, etc.).

    Attributes:
        name: Module name.
        module_type: One of terraform, helm, python_package.
        summary: Concise description of what the module does.
        provisions: Resources or capabilities the module creates.
        consumed_by: Hints about consumers of this module.
        inputs: Key input variables or parameters.
        outputs: Key exported values or resources.
        tags: Semantic tags for search.
        cost_usd: Estimated USD cost of the analysis call.
    """

    name: str
    module_type: str
    summary: str
    provisions: list[str] = field(default_factory=list)
    consumed_by: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class ServiceInteraction:
    """Describes a directional communication path between two services.

    Attributes:
        source: Name of the calling/producing service.
        target: Name of the callee/consuming service.
        protocol: Transport — http, grpc, sqs, sns, kafka, s3, or dynamodb.
        pattern: Interaction pattern — sync, async, event, or polling.
        description: Human-readable description of the interaction.
        confidence: LLM confidence (0.0–1.0).
    """

    source: str
    target: str
    protocol: str
    pattern: str
    description: str
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class CodeUnderstanding:
    """Async client for LLM-powered semantic analysis of code.

    Uses AWS Bedrock (Haiku for per-repo analysis, Sonnet for cross-service
    interaction analysis). Concurrency is bounded by a semaphore. A per-run
    budget cap prevents runaway spend during large indexing operations.

    Graceful degradation: if Bedrock is permanently unavailable the
    ``_available`` flag is set to False and callers receive minimal-data
    fallback objects rather than raised exceptions.

    Usage::

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.analyze_repo(r)) for r in repos]
    """

    def __init__(self, config: Optional[UnderstandingConfig] = None) -> None:
        if config is None:
            config = self._config_from_platform()
        self.config = config
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._budget_used: float = 0.0
        self._available: bool = True
        self._client = self._init_client()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_repo(self, repo_summary: dict) -> RepoUnderstanding:
        """Generate semantic understanding for a single repository.

        Args:
            repo_summary: Dict with keys: name, path, purpose, tech_stack,
                depends_on, key_files, readme_content, sample_code.

        Returns:
            RepoUnderstanding with architectural summary, pattern, domain,
            complexity, exposes/consumes lists, and tags.
        """
        name = repo_summary.get("name", "unknown")
        prompt = self._build_repo_prompt(repo_summary)
        try:
            raw = await self._invoke_model(prompt, self.config.analysis_model)
            understanding = self._parse_repo_response(raw, name)
            understanding.model_used = self.config.analysis_model
            logger.debug("analyze_repo: OK repo=%s pattern=%s domain=%s",
                         name, understanding.architectural_pattern, understanding.domain)
            return understanding
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning("analyze_repo: failed for %s, returning fallback: %s", name, exc)
            return RepoUnderstanding(
                name=name,
                summary="Analysis unavailable.",
                architectural_pattern="unknown",
                domain="unknown",
                complexity=1,
                confidence=0.0,
                model_used=self.config.analysis_model,
                cost_usd=0.0,
            )

    async def analyze_module(
        self,
        module_content: str,
        module_type: str,
        context: str,
    ) -> ModuleUnderstanding:
        """Generate semantic understanding for a reusable module.

        Args:
            module_content: Raw source content of the module (Terraform HCL,
                Helm chart YAML, Python package, etc.).
            module_type: One of terraform, helm, python_package.
            context: Short description of where/how this module is used.

        Returns:
            ModuleUnderstanding with provisions, inputs, outputs, and tags.
        """
        name = context or module_type
        prompt = self._build_module_prompt(module_content, module_type, context)
        try:
            raw = await self._invoke_model(prompt, self.config.analysis_model)
            understanding = self._parse_module_response(raw, name, module_type)
            logger.debug("analyze_module: OK name=%s type=%s", name, module_type)
            return understanding
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning("analyze_module: failed for %s, returning fallback: %s", name, exc)
            return ModuleUnderstanding(
                name=name,
                module_type=module_type,
                summary="Analysis unavailable.",
                cost_usd=0.0,
            )

    async def analyze_service_interactions(
        self,
        services: list[dict],
    ) -> list[ServiceInteraction]:
        """Identify communication paths between multiple services.

        Uses the complex model (Sonnet) because cross-service reasoning
        requires broader context understanding than single-repo analysis.

        Args:
            services: List of service dicts, each with at minimum a ``name``
                key. Additional keys (purpose, tech_stack, exposes, consumes)
                improve accuracy.

        Returns:
            List of ServiceInteraction instances describing directional
            communication paths found in the service set.
        """
        if not services:
            return []
        prompt = self._build_interactions_prompt(services)
        try:
            raw = await self._invoke_model(prompt, self.config.complex_model)
            interactions = self._parse_interactions_response(raw)
            logger.info(
                "analyze_service_interactions: found %d interactions for %d services",
                len(interactions),
                len(services),
            )
            return interactions
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning("analyze_service_interactions: failed, returning empty: %s", exc)
            return []

    async def generate_embeddings_text(self, understanding: RepoUnderstanding) -> str:
        """Generate optimised text for embedding from a RepoUnderstanding.

        Concatenates summary, pattern, domain, tags, and relationship hints
        into a single dense string designed for semantic similarity search.

        Args:
            understanding: A populated RepoUnderstanding instance.

        Returns:
            A flat string suitable for passage to the embeddings client.
        """
        parts: list[str] = [
            understanding.summary,
            f"Pattern: {understanding.architectural_pattern}.",
            f"Domain: {understanding.domain}.",
            f"Complexity: {understanding.complexity}/5.",
        ]
        if understanding.exposes:
            parts.append("Exposes: " + ", ".join(understanding.exposes) + ".")
        if understanding.consumes:
            parts.append("Consumes: " + ", ".join(understanding.consumes) + ".")
        if understanding.tags:
            parts.append("Tags: " + " ".join(understanding.tags) + ".")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Availability and budget helpers
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if the Bedrock client initialised successfully."""
        return self._available

    @property
    def budget_used(self) -> float:
        """Total USD spent in this session."""
        return round(self._budget_used, 6)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_client(self) -> Optional[object]:
        """Initialise the boto3 bedrock-runtime client (eager, not lazy)."""
        session_kwargs: dict = {"region_name": self.config.region}
        if self.config.profile:
            session_kwargs["profile_name"] = self.config.profile
        try:
            session = boto3.Session(**session_kwargs)
            return session.client("bedrock-runtime")
        except Exception as exc:
            logger.warning(
                "Failed to initialise Bedrock client — code understanding unavailable: %s", exc
            )
            self._available = False
            return None

    @staticmethod
    def _config_from_platform() -> UnderstandingConfig:
        """Load UnderstandingConfig from PlatformConfig / BedrockConfig."""
        try:
            from cap.lib.config import load_config
            pconfig = load_config()
            b = pconfig.bedrock
            return UnderstandingConfig(
                region=b.region,
                profile=b.profile,
                max_retries=b.max_retries,
            )
        except Exception:
            return UnderstandingConfig()

    def _ensure_semaphore(self) -> asyncio.Semaphore:
        """Lazily create the semaphore on first use (event-loop safe)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.config.max_concurrent)
        return self._semaphore

    async def _invoke_model(self, prompt: str, model_id: str) -> str:
        """Invoke a Bedrock model with exponential back-off and budget tracking.

        Args:
            prompt: Full user prompt text.
            model_id: Bedrock model identifier.

        Returns:
            The model's text response.

        Raises:
            BudgetExceeded: If accumulated spend reaches config.budget_limit_usd.
            RuntimeError: If all retry attempts are exhausted.
        """
        if self._client is None:
            raise RuntimeError("Bedrock client is not available.")

        # Pre-flight budget estimate (input tokens only; output estimated separately)
        est_input_cost = self._estimate_cost(prompt, "", model_id)
        if self._budget_used + est_input_cost > self.config.budget_limit_usd:
            raise BudgetExceeded(
                f"Budget limit ${self.config.budget_limit_usd:.4f} would be exceeded "
                f"(used=${self._budget_used:.4f}, estimated=${est_input_cost:.4f})."
            )

        truncated_prompt = prompt[: self.config.max_input_chars]
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": truncated_prompt}],
        })

        semaphore = self._ensure_semaphore()
        last_exc: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                async with semaphore:
                    response = await asyncio.to_thread(
                        self._client.invoke_model,
                        modelId=model_id,
                        body=body,
                        contentType="application/json",
                        accept="application/json",
                    )
                text: str = json.loads(response["body"].read())["content"][0]["text"]

                # Track actual cost based on output length approximation
                actual_cost = self._estimate_cost(truncated_prompt, text, model_id)
                self._budget_used += actual_cost
                logger.debug(
                    "_invoke_model: OK model=%s attempt=%d cost=$%.6f total=$%.6f",
                    model_id,
                    attempt + 1,
                    actual_cost,
                    self._budget_used,
                )
                return text

            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in _RETRYABLE_ERROR_CODES:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Bedrock throttled (%s), retrying in %.1fs (attempt %d/%d)",
                        code,
                        delay,
                        attempt + 1,
                        self.config.max_retries,
                    )
                    await asyncio.sleep(delay)
                    last_exc = exc
                elif code in _PERMANENT_ERROR_CODES:
                    self._available = False
                    logger.error("Bedrock permanent error %s: %s", code, exc)
                    raise RuntimeError(f"Bedrock permanent error {code}: {exc}") from exc
                else:
                    logger.error("Bedrock unexpected error code=%s: %s", code, exc)
                    raise RuntimeError(f"Bedrock error {code}: {exc}") from exc

            except Exception as exc:
                logger.error("_invoke_model: unexpected error: %s", exc, exc_info=True)
                raise

        raise RuntimeError(
            f"_invoke_model: all {self.config.max_retries} attempts exhausted."
        ) from last_exc

    def _build_repo_prompt(self, repo_data: dict) -> str:
        """Construct a structured prompt for repository analysis.

        Args:
            repo_data: Dict with repo metadata and content samples.

        Returns:
            Prompt string requesting JSON output.
        """
        name = repo_data.get("name", "unknown")
        purpose = repo_data.get("purpose", "")
        tech_stack = repo_data.get("tech_stack", [])
        depends_on = repo_data.get("depends_on", [])
        key_files = repo_data.get("key_files", [])
        readme = (repo_data.get("readme_content") or "")[:3000]
        sample_code = (repo_data.get("sample_code") or "")[:3000]

        tech_str = ", ".join(tech_stack) if isinstance(tech_stack, list) else str(tech_stack)
        deps_str = ", ".join(depends_on) if isinstance(depends_on, list) else str(depends_on)
        files_str = ", ".join(key_files) if isinstance(key_files, list) else str(key_files)

        return f"""Analyse this software repository and respond ONLY with valid JSON matching the schema below.

Repository: {name}
Purpose: {purpose}
Tech stack: {tech_str}
Dependencies: {deps_str}
Key files: {files_str}
README excerpt:
{readme}
Sample code:
{sample_code}

Required JSON schema:
{{
  "summary": "<2-3 sentence architectural description>",
  "architectural_pattern": "<one of: event-driven|request-response|pub-sub|batch|cron|library|CLI>",
  "domain": "<one of: infrastructure|platform|application|tooling|observability|data|security>",
  "complexity": <integer 1-5>,
  "exposes": ["<API/event/resource>", ...],
  "consumes": ["<external service/API>", ...],
  "tags": ["<search tag>", ...],
  "confidence": <float 0.0-1.0>
}}

Respond with JSON only. No explanation, no markdown fences."""

    def _parse_repo_response(self, response: str, repo_name: str) -> RepoUnderstanding:
        """Parse the JSON response from the LLM for repo analysis.

        Falls back to regex extraction if the response is not valid JSON.

        Args:
            response: Raw text from the Bedrock model.
            repo_name: Repository name (used as fallback identifier).

        Returns:
            RepoUnderstanding populated from the response.
        """
        data = self._extract_json(response)
        if data is None:
            logger.warning(
                "_parse_repo_response: JSON parse failed for %s, using regex fallback",
                repo_name,
            )
            data = self._regex_extract_repo(response)

        def _str_list(val: object) -> list[str]:
            if isinstance(val, list):
                return [str(v) for v in val]
            if isinstance(val, str):
                return [val] if val else []
            return []

        complexity = int(data.get("complexity", 1))
        complexity = max(1, min(5, complexity))

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return RepoUnderstanding(
            name=repo_name,
            summary=str(data.get("summary", "No summary available.")),
            architectural_pattern=str(data.get("architectural_pattern", "unknown")),
            domain=str(data.get("domain", "unknown")),
            complexity=complexity,
            exposes=_str_list(data.get("exposes", [])),
            consumes=_str_list(data.get("consumes", [])),
            tags=_str_list(data.get("tags", [])),
            confidence=confidence,
            cost_usd=0.0,  # set by caller after _invoke_model
        )

    def _build_module_prompt(
        self,
        module_content: str,
        module_type: str,
        context: str,
    ) -> str:
        """Construct a prompt for module analysis.

        Args:
            module_content: Raw module source content.
            module_type: terraform, helm, or python_package.
            context: Brief usage context.

        Returns:
            Prompt string requesting JSON output.
        """
        content_excerpt = module_content[: self.config.max_input_chars - 1000]
        return f"""Analyse this {module_type} module and respond ONLY with valid JSON.

Context: {context}
Content:
{content_excerpt}

Required JSON schema:
{{
  "summary": "<concise description of what this module does>",
  "provisions": ["<resource or capability created>", ...],
  "consumed_by": ["<hint about consumers>", ...],
  "inputs": ["<key input variable>", ...],
  "outputs": ["<key output/export>", ...],
  "tags": ["<search tag>", ...]
}}

Respond with JSON only. No explanation, no markdown fences."""

    def _parse_module_response(
        self,
        response: str,
        name: str,
        module_type: str,
    ) -> ModuleUnderstanding:
        """Parse the JSON response from the LLM for module analysis.

        Args:
            response: Raw text from the Bedrock model.
            name: Module name / context.
            module_type: terraform, helm, or python_package.

        Returns:
            ModuleUnderstanding populated from the response.
        """
        data = self._extract_json(response)
        if data is None:
            logger.warning(
                "_parse_module_response: JSON parse failed for %s, using empty fallback", name
            )
            data = {}

        def _str_list(val: object) -> list[str]:
            if isinstance(val, list):
                return [str(v) for v in val]
            if isinstance(val, str):
                return [val] if val else []
            return []

        return ModuleUnderstanding(
            name=name,
            module_type=module_type,
            summary=str(data.get("summary", "No summary available.")),
            provisions=_str_list(data.get("provisions", [])),
            consumed_by=_str_list(data.get("consumed_by", [])),
            inputs=_str_list(data.get("inputs", [])),
            outputs=_str_list(data.get("outputs", [])),
            tags=_str_list(data.get("tags", [])),
            cost_usd=0.0,
        )

    def _build_interactions_prompt(self, services: list[dict]) -> str:
        """Construct a prompt for cross-service interaction analysis.

        Args:
            services: List of service descriptor dicts.

        Returns:
            Prompt string requesting a JSON array output.
        """
        services_json = json.dumps(services, indent=2)[: self.config.max_input_chars - 500]
        return f"""Analyse the communication paths between these services and respond ONLY with a JSON array.

Services:
{services_json}

For each directional interaction you can infer, return one object:
{{
  "source": "<service name>",
  "target": "<service name>",
  "protocol": "<one of: http|grpc|sqs|sns|kafka|s3|dynamodb>",
  "pattern": "<one of: sync|async|event|polling>",
  "description": "<short description of the interaction>",
  "confidence": <float 0.0-1.0>
}}

Return a JSON array of such objects. If no interactions are detectable, return [].
Respond with JSON only. No explanation, no markdown fences."""

    def _parse_interactions_response(self, response: str) -> list[ServiceInteraction]:
        """Parse the JSON array response for service interactions.

        Args:
            response: Raw text from the Bedrock model.

        Returns:
            List of ServiceInteraction instances.
        """
        data = self._extract_json(response)
        if not isinstance(data, list):
            logger.warning(
                "_parse_interactions_response: expected list, got %s — returning empty",
                type(data).__name__,
            )
            return []

        interactions: list[ServiceInteraction] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", ""))
            target = str(item.get("target", ""))
            if not source or not target:
                continue
            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            interactions.append(ServiceInteraction(
                source=source,
                target=target,
                protocol=str(item.get("protocol", "unknown")),
                pattern=str(item.get("pattern", "unknown")),
                description=str(item.get("description", "")),
                confidence=confidence,
            ))
        return interactions

    def _estimate_cost(self, prompt: str, response: str, model_id: str) -> float:
        """Estimate the USD cost of a single Bedrock call.

        Uses the 4 chars-per-token heuristic for both input and output.

        Args:
            prompt: Input text.
            response: Output text (empty string for pre-flight estimate).
            model_id: Bedrock model identifier — determines price per token.

        Returns:
            Estimated cost in USD.
        """
        input_tokens = max(0, len(prompt) // 4)
        output_tokens = max(0, len(response) // 4)

        is_sonnet = "sonnet" in model_id.lower()
        cost_in = self.config.cost_per_1k_input_sonnet if is_sonnet else self.config.cost_per_1k_input_haiku
        cost_out = self.config.cost_per_1k_output_sonnet if is_sonnet else self.config.cost_per_1k_output_haiku

        return (input_tokens * cost_in / 1000.0) + (output_tokens * cost_out / 1000.0)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential back-off delay for Bedrock throttle retries.

        Args:
            attempt: Zero-based attempt index.

        Returns:
            Delay in seconds, derived from BedrockConfig base_delay_ms if
            available, otherwise a 0.5 s base.
        """
        try:
            from cap.lib.config import load_config
            b = load_config().bedrock
            base = b.base_delay_ms / 1000.0
            multiplier = b.backoff_multiplier
            max_delay = b.max_delay_ms / 1000.0
        except Exception:
            base = 0.5
            multiplier = 2.0
            max_delay = 10.0

        delay = base * (multiplier ** attempt)
        return min(delay, max_delay)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict | list]:
        """Extract and parse the first JSON object or array from text.

        Tries three strategies:
        1. Direct ``json.loads`` of the whole string.
        2. Strip markdown fences and retry.
        3. Regex extraction of the first ``{...}`` or ``[...]`` block.

        Args:
            text: Raw LLM response text.

        Returns:
            Parsed dict or list, or None if all strategies fail.
        """
        stripped = text.strip()

        # Strategy 1: direct parse
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        # Strategy 2: strip markdown code fences
        fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
        match = fence_pattern.search(stripped)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Strategy 3: extract first {...} or [...] block
        obj_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", stripped)
        if obj_match:
            try:
                return json.loads(obj_match.group(1))
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
    def _regex_extract_repo(text: str) -> dict:
        """Best-effort key extraction from a non-JSON LLM response.

        Used as a last resort when ``_extract_json`` fails. Looks for
        recognisable field names followed by their values.

        Args:
            text: Raw LLM response text.

        Returns:
            Partial dict with whatever fields could be extracted.
        """
        data: dict = {}

        summary_match = re.search(r"summary[\":\s]+([^\n\"{]+)", text, re.IGNORECASE)
        if summary_match:
            data["summary"] = summary_match.group(1).strip().rstrip(",")

        pattern_match = re.search(
            r"(event-driven|request-response|pub-sub|batch|cron|library|CLI)",
            text,
            re.IGNORECASE,
        )
        if pattern_match:
            data["architectural_pattern"] = pattern_match.group(1).lower()

        domain_match = re.search(
            r"(infrastructure|platform|application|tooling|observability|data|security)",
            text,
            re.IGNORECASE,
        )
        if domain_match:
            data["domain"] = domain_match.group(1).lower()

        complexity_match = re.search(r"complexity[\":\s]+([1-5])", text, re.IGNORECASE)
        if complexity_match:
            data["complexity"] = int(complexity_match.group(1))

        confidence_match = re.search(r"confidence[\":\s]+(0\.\d+|1\.0)", text, re.IGNORECASE)
        if confidence_match:
            data["confidence"] = float(confidence_match.group(1))

        return data

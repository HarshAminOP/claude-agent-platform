"""Embeddings via AWS Bedrock Titan Text Embeddings V2.

Titan V2 does not accept batch input — each text requires a separate invoke_model
call. Parallelism is achieved with asyncio.gather + a semaphore to cap concurrent
Bedrock calls. On permanent Bedrock unavailability the client returns None so
callers can fall back to FTS5-only search without raising.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("cap.embeddings")

# Error codes that indicate a permanent, non-retryable Bedrock failure.
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


@dataclass
class EmbeddingConfig:
    """Configuration for the Bedrock Titan Text Embeddings V2 client.

    Attributes:
        model_id: Bedrock model identifier for Titan V2.
        dimensions: Output vector dimensions (256, 512, or 1024).
        normalize: Whether Titan should L2-normalise the output vector.
        max_input_tokens: Maximum tokens accepted by the model; used to
            estimate a safe character truncation limit.
        max_concurrent: Maximum simultaneous in-flight Bedrock calls.
        cost_per_million_tokens: USD cost per 1 M input tokens (Titan V2
            pricing as of model release; update if AWS changes it).
        max_retries: Maximum attempts per text before returning None.
        base_delay_s: Initial back-off delay in seconds on throttle.
        max_delay_s: Upper bound for exponential back-off delay.
        backoff_multiplier: Multiplier applied to delay on each retry.
        region: AWS region for the Bedrock Runtime endpoint.
        profile: Optional AWS named profile; None uses the ambient credential
            chain (instance role, env vars, etc.).
    """

    model_id: str = "amazon.titan-embed-text-v2:0"
    dimensions: int = 1024
    normalize: bool = True
    max_input_tokens: int = 8192
    max_concurrent: int = 3
    cost_per_million_tokens: float = 0.02
    max_retries: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 10.0
    backoff_multiplier: float = 2.0
    region: str = "us-east-1"
    profile: Optional[str] = None


class EmbeddingClient:
    """Async client for generating embeddings via Bedrock Titan Text V2.

    Thread-safety: the underlying boto3 client is created once and shared
    across coroutines. boto3 clients are thread-safe for individual method
    calls, and asyncio.to_thread ensures calls do not block the event loop.

    Graceful degradation: if Bedrock is unavailable (AccessDenied, model not
    ready, unrecoverable service error) ``embed_single`` returns ``None``
    rather than raising. Callers should check ``is_available`` before relying
    on embedding results and fall back to full-text search when it is False.
    """

    def __init__(self, config: EmbeddingConfig = None) -> None:
        self._fallback_provider: str = "sentence-transformers"
        self._fallback_model: str = "all-MiniLM-L6-v2"
        self._fallback_client: Optional["SentenceTransformerClient"] = None

        if config is None:
            try:
                from cap.lib.harness_config import get_embeddings_config
                emb_cfg = get_embeddings_config()
                config = EmbeddingConfig(
                    model_id=emb_cfg.get("model_id", "amazon.titan-embed-text-v2:0"),
                    dimensions=emb_cfg.get("dimensions", 1024),
                    region=emb_cfg.get("region", "us-east-1"),
                    profile=emb_cfg.get("profile"),
                )
                self._fallback_provider = emb_cfg.get("fallback", "sentence-transformers")
                self._fallback_model = emb_cfg.get("fallback_model", "all-MiniLM-L6-v2")
            except Exception:
                config = EmbeddingConfig()
                self._fallback_provider = "sentence-transformers"
                self._fallback_model = "all-MiniLM-L6-v2"
        self.config = config
        self._semaphore: Optional[asyncio.Semaphore] = None
        # None = not tested yet, True = last call succeeded, False = unavailable
        self._available: Optional[bool] = None

        session_kwargs: dict = {"region_name": self.config.region}
        if self.config.profile:
            session_kwargs["profile_name"] = self.config.profile

        try:
            session = boto3.Session(**session_kwargs)
            self._client = session.client("bedrock-runtime")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to initialise Bedrock client — embeddings unavailable: %s", exc
            )
            self._client = None
            self._available = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed_single(self, text: str) -> Optional[list[float]]:
        """Embed a single text string using Titan V2.

        Applies character-level truncation before the call and retries with
        exponential back-off on throttle responses. Permanent failures are
        logged and result in a ``None`` return rather than a raised exception,
        enabling callers to degrade gracefully to FTS5-only search.

        Args:
            text: The text to embed. Empty strings are returned as None.

        Returns:
            A list of floats (the embedding vector) on success, or None on
            any unrecoverable failure.
        """
        if self._client is None:
            return await self._fallback_embed(text)

        if self._available is False:
            return await self._fallback_embed(text)

        if not text or not text.strip():
            logger.debug("embed_single: empty text, returning None")
            return None

        text = self._truncate(text)
        body = self._build_request_body(text)

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.config.max_concurrent)

        for attempt in range(self.config.max_retries):
            try:
                async with self._semaphore:
                    response = await asyncio.to_thread(
                        self._client.invoke_model,
                        modelId=self.config.model_id,
                        body=body,
                        contentType="application/json",
                        accept="application/json",
                    )

                result = json.loads(response["body"].read())
                embedding: list[float] = result["embedding"]
                self._available = True
                logger.debug(
                    "embed_single: OK dim=%d attempt=%d", len(embedding), attempt + 1
                )
                return embedding

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
                    # continue to next attempt

                elif code in _PERMANENT_ERROR_CODES:
                    logger.error(
                        "Bedrock permanent error %s — switching to fallback: %s",
                        code,
                        exc,
                    )
                    self._available = False
                    return await self._fallback_embed(text)

                else:
                    logger.error(
                        "Bedrock unexpected ClientError code=%s: %s", code, exc
                    )
                    return None

            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected embedding error: %s", exc, exc_info=True)
                return None

        logger.warning(
            "embed_single: all %d attempts exhausted, returning None",
            self.config.max_retries,
        )
        return None

    async def embed_batch(
        self, texts: list[str]
    ) -> list[Optional[list[float]]]:
        """Embed multiple texts in parallel with bounded concurrency.

        Each text is submitted as an independent Bedrock call. The semaphore
        inside ``embed_single`` ensures at most ``config.max_concurrent`` calls
        are in flight simultaneously.

        Args:
            texts: List of strings to embed. May contain duplicates.

        Returns:
            A list of the same length as ``texts``. Each element is either a
            float vector on success or ``None`` if that text failed.
        """
        if not texts:
            return []

        tasks = [self.embed_single(t) for t in texts]
        results: list[Optional[list[float]]] = await asyncio.gather(*tasks)
        success = sum(1 for r in results if r is not None)
        logger.debug(
            "embed_batch: %d/%d succeeded", success, len(texts)
        )
        return results

    # ------------------------------------------------------------------
    # Availability and cost helpers
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> Optional[bool]:
        """Availability state derived from the most recent call.

        Returns:
            True  — at least one call has succeeded.
            False — Bedrock reported a permanent unavailability error.
            None  — no call has been made yet (state is unknown).
        """
        return self._available

    def estimate_tokens(self, text: str) -> int:
        """Rough token count estimate using the 4 chars-per-token heuristic.

        This is intentionally conservative; actual tokenisation may differ.
        Use only for cost estimation, not for correctness-critical truncation.

        Args:
            text: Input text.

        Returns:
            Estimated token count (integer, ≥ 0).
        """
        return max(0, len(text) // 4)

    def estimate_cost(self, texts: list[str]) -> float:
        """Estimate the total embedding cost in USD for a list of texts.

        Uses ``estimate_tokens`` internally, so the result is approximate.

        Args:
            texts: Texts to cost-estimate.

        Returns:
            Estimated cost in USD, rounded to 8 decimal places.
        """
        total_tokens = sum(self.estimate_tokens(t) for t in texts)
        cost = total_tokens * self.config.cost_per_million_tokens / 1_000_000
        return round(cost, 8)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _truncate(self, text: str) -> str:
        """Truncate text to stay within the model's token budget.

        Uses 3 chars/token (conservative) to avoid ValidationException from Titan.
        """
        max_chars = self.config.max_input_tokens * 3
        if len(text) > max_chars:
            logger.debug(
                "Truncating text from %d to %d chars", len(text), max_chars
            )
            return text[:max_chars]
        return text

    def _build_request_body(self, text: str) -> str:
        """Serialise the Bedrock invoke_model request body for Titan V2."""
        return json.dumps(
            {
                "inputText": text,
                "dimensions": self.config.dimensions,
                "normalize": self.config.normalize,
            }
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Calculate the exponential back-off delay for the given attempt index.

        Delay is capped at ``config.max_delay_s``.
        """
        delay = self.config.base_delay_s * (self.config.backoff_multiplier ** attempt)
        return min(delay, self.config.max_delay_s)

    async def _fallback_embed(self, text: str) -> Optional[list[float]]:
        """Attempt to embed using the configured fallback provider.

        Initializes the fallback client lazily on first use. Returns None
        if the fallback is unavailable or fails.
        """
        if self._fallback_provider != "sentence-transformers":
            return None

        if self._fallback_client is None:
            self._fallback_client = SentenceTransformerClient(
                model_name=self._fallback_model
            )

        if not self._fallback_client.is_available:
            return None

        try:
            return await asyncio.to_thread(
                self._fallback_client.embed_single_sync, text
            )
        except Exception as exc:
            logger.warning("Fallback embedding failed: %s", exc)
            return None


class SentenceTransformerClient:
    """Local embedding fallback using sentence-transformers library.

    This client uses the sentence-transformers package to generate embeddings
    locally without any API calls. It is used as a fallback when Bedrock is
    unavailable (AccessDenied, ServiceUnavailable, etc.).

    The sentence-transformers package is an OPTIONAL dependency. If not
    installed, the client reports itself as unavailable and all methods
    return None gracefully.

    Usage::

        client = SentenceTransformerClient(model_name="all-MiniLM-L6-v2")
        if client.is_available:
            vector = client.embed_single_sync("hello world")
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Initialize the sentence-transformers client.

        Args:
            model_name: HuggingFace model identifier. Default is
                all-MiniLM-L6-v2 which produces 384-dim vectors and is ~80MB.
        """
        self._model_name = model_name
        self._model = None
        self._available: bool = False

        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self._available = True
            logger.info(
                "SentenceTransformerClient initialized: model=%s", model_name
            )
        except ImportError:
            logger.info(
                "sentence-transformers not installed — local fallback unavailable. "
                "Install with: pip install sentence-transformers"
            )
        except Exception as exc:
            logger.warning(
                "SentenceTransformerClient init failed: %s", exc
            )

    @property
    def is_available(self) -> bool:
        """Whether the sentence-transformers model loaded successfully."""
        return self._available

    def embed_single_sync(self, text: str) -> Optional[list[float]]:
        """Embed a single text synchronously.

        Args:
            text: The text to embed. Empty strings return None.

        Returns:
            A list of floats (the embedding vector) or None on failure.
        """
        if not self._available or self._model is None:
            return None

        if not text or not text.strip():
            return None

        try:
            embedding = self._model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
        except Exception as exc:
            logger.warning(
                "SentenceTransformer encode failed: %s", exc
            )
            return None

    def embed_batch_sync(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed multiple texts synchronously in a single batch.

        The sentence-transformers library handles batching internally
        which is more efficient than calling encode() one at a time.

        Args:
            texts: List of strings to embed.

        Returns:
            List of embedding vectors (or None for failed texts).
        """
        if not self._available or self._model is None:
            return [None] * len(texts)

        if not texts:
            return []

        try:
            embeddings = self._model.encode(texts, convert_to_numpy=True)
            return [emb.tolist() for emb in embeddings]
        except Exception as exc:
            logger.warning(
                "SentenceTransformer batch encode failed: %s", exc
            )
            return [None] * len(texts)

    @property
    def dimensions(self) -> int:
        """Return the output dimensionality of the loaded model.

        Returns 384 for all-MiniLM-L6-v2, or 0 if model is unavailable.
        """
        if self._model is not None:
            try:
                return self._model.get_sentence_embedding_dimension()
            except Exception:
                pass
        return 0

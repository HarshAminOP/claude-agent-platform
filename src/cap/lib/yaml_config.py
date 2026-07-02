"""YAML configuration file parser for the CAP platform.

Provides :func:`parse_yaml_config` as the primary entry point for loading
a YAML file from disk and returning a validated :class:`YamlConfig` object.

Design notes
------------
* Uses ``PyYAML`` (``yaml.safe_load``) — *not* ``yaml.load``, which would
  allow arbitrary Python object construction and is a known security risk.
* Validated with **Pydantic v2** so that callers receive a typed, coerced
  model rather than a raw ``dict``.  Unknown top-level keys are ignored by
  default (``model_config = ConfigDict(extra="ignore")``).
* ``parse_yaml_config`` never raises for a missing file — it returns
  ``YamlConfig()`` with all defaults, matching the precedent set in
  :mod:`cap.config` (``load()`` returns ``Config()`` when the file is
  absent).
* Multi-document YAML files (streams with ``---`` separators) are
  explicitly rejected; only a single-document mapping is accepted.
* All I/O errors and YAML parse errors surface as :exc:`YamlConfigError`
  so callers have a single exception type to handle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class YamlConfigError(Exception):
    """Raised when a YAML config file cannot be read or parsed.

    Attributes
    ----------
    path:
        The file path that triggered the error, or ``None`` when the error
        originates from in-memory content (e.g. tests).
    """

    def __init__(self, message: str, path: Path | None = None) -> None:
        self.path = path
        super().__init__(message)


# ---------------------------------------------------------------------------
# Typed sub-models (mirrors the structure used in lib/config.py)
# ---------------------------------------------------------------------------


class YamlServiceConfig(BaseModel):
    """Service-level settings embedded inside a YAML config."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    port: int = Field(default=8080, ge=1, le=65535)
    debug: bool = False
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(
                f"log_level must be one of {sorted(allowed)!r}, got {v!r}"
            )
        return upper


class YamlDatabaseConfig(BaseModel):
    """Database connection settings embedded inside a YAML config."""

    model_config = ConfigDict(extra="ignore")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = ""
    pool_size: int = Field(default=5, ge=1)
    pool_timeout_seconds: int = Field(default=30, ge=1)


class YamlConfig(BaseModel):
    """Top-level validated configuration loaded from a YAML file.

    All sections are optional; absent keys fall back to typed defaults.
    Unknown top-level keys are silently ignored so that config files can
    carry application-specific sections not modelled here.

    Attributes
    ----------
    version:
        Optional schema version string for the config file itself.
    service:
        Service identity and runtime flags.
    database:
        Database connection parameters.
    extra_vars:
        Catch-all mapping for caller-defined keys outside the typed schema.
        Populated by :func:`parse_yaml_config` from the raw ``dict`` before
        Pydantic validation runs.
    """

    model_config = ConfigDict(extra="ignore")

    version: str = ""
    service: YamlServiceConfig = Field(default_factory=YamlServiceConfig)
    database: YamlDatabaseConfig = Field(default_factory=YamlDatabaseConfig)
    extra_vars: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_yaml_config(path: Path | str) -> YamlConfig:
    """Parse a YAML configuration file and return a validated :class:`YamlConfig`.

    The function gracefully handles a missing file by returning a
    ``YamlConfig()`` instance with all defaults — consistent with the
    pattern used in :func:`cap.config.load`.

    Parameters
    ----------
    path:
        Absolute or relative path to the YAML file (``*.yaml`` / ``*.yml``).

    Returns
    -------
    YamlConfig
        A fully validated configuration object.

    Raises
    ------
    YamlConfigError
        * If the file exists but cannot be opened (permissions, I/O error).
        * If the file content is not valid YAML.
        * If the top-level YAML value is not a mapping (e.g. it is a list
          or a bare scalar).
        * If the file contains multiple YAML documents (``---`` separator).
        * If a typed field fails Pydantic validation (e.g. ``port: "abc"``).
    """
    resolved = Path(path)

    if not resolved.exists():
        logger.debug("yaml_config: %s not found, returning defaults", resolved)
        return YamlConfig()

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlConfigError(
            f"Cannot read YAML config file '{resolved}': {exc}", path=resolved
        ) from exc

    return _parse_yaml_string(raw_text, source_path=resolved)


def _parse_yaml_string(content: str, *, source_path: Path | None = None) -> YamlConfig:
    """Parse YAML from a raw string and return a validated :class:`YamlConfig`.

    This is the internal workhorse used by both :func:`parse_yaml_config`
    (file-based) and the test suite (in-memory strings).

    Parameters
    ----------
    content:
        Raw YAML text.
    source_path:
        Optional path attached to :exc:`YamlConfigError` for diagnostics.

    Returns
    -------
    YamlConfig

    Raises
    ------
    YamlConfigError
        See :func:`parse_yaml_config` for the full list of error conditions.
    """
    # Detect multi-document streams before attempting a full parse.
    # yaml.safe_load_all returns a generator; we check that exactly one
    # document is present to avoid silently discarding subsequent documents.
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        raise YamlConfigError(
            f"Invalid YAML syntax: {exc}", path=source_path
        ) from exc

    # Filter out ``None`` docs produced by trailing ``---`` separators.
    non_empty = [d for d in docs if d is not None]

    if len(non_empty) == 0:
        # Empty file — return all defaults.
        return YamlConfig()

    if len(non_empty) > 1:
        raise YamlConfigError(
            f"YAML config must contain exactly one document; "
            f"found {len(non_empty)} non-empty documents. "
            "Split multi-document files before loading.",
            path=source_path,
        )

    raw: Any = non_empty[0]

    if not isinstance(raw, dict):
        raise YamlConfigError(
            f"YAML config root must be a mapping (dict), got {type(raw).__name__!r}. "
            "Ensure the file starts with key: value pairs, not a list or scalar.",
            path=source_path,
        )

    # Separate known top-level keys from extra caller-defined keys.
    known_keys = {"version", "service", "database"}
    extra_vars = {k: v for k, v in raw.items() if k not in known_keys}
    config_data = {k: v for k, v in raw.items() if k in known_keys}
    config_data["extra_vars"] = extra_vars

    try:
        from pydantic import ValidationError

        return YamlConfig.model_validate(config_data)
    except Exception as exc:  # ValidationError is a subclass of ValueError
        raise YamlConfigError(
            f"YAML config validation failed: {exc}", path=source_path
        ) from exc

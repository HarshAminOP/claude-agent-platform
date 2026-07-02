"""Tests for cap.lib.yaml_config — YAML config file parser."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cap.lib.yaml_config import (
    YamlConfig,
    YamlConfigError,
    YamlDatabaseConfig,
    YamlServiceConfig,
    _parse_yaml_string,
    parse_yaml_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _parse_yaml_string — unit tests (no filesystem required)
# ---------------------------------------------------------------------------


class TestParseYamlString:
    """Tests for the internal _parse_yaml_string helper."""

    def test_empty_content_returns_defaults(self):
        cfg = _parse_yaml_string("")
        assert isinstance(cfg, YamlConfig)
        assert cfg.version == ""
        assert cfg.service.port == 8080
        assert cfg.service.log_level == "INFO"
        assert cfg.database.host == "localhost"
        assert cfg.extra_vars == {}

    def test_minimal_valid_mapping(self):
        cfg = _parse_yaml_string("version: '1.0'")
        assert cfg.version == "1.0"

    def test_full_service_section(self):
        yaml_text = """
version: "2"
service:
  name: my-service
  port: 9090
  debug: true
  log_level: debug
"""
        cfg = _parse_yaml_string(yaml_text)
        assert cfg.version == "2"
        assert cfg.service.name == "my-service"
        assert cfg.service.port == 9090
        assert cfg.service.debug is True
        assert cfg.service.log_level == "DEBUG"  # normalised to upper

    def test_full_database_section(self):
        yaml_text = """
database:
  host: db.internal
  port: 3306
  name: mydb
  pool_size: 10
  pool_timeout_seconds: 60
"""
        cfg = _parse_yaml_string(yaml_text)
        assert cfg.database.host == "db.internal"
        assert cfg.database.port == 3306
        assert cfg.database.name == "mydb"
        assert cfg.database.pool_size == 10
        assert cfg.database.pool_timeout_seconds == 60

    def test_unknown_top_level_keys_go_to_extra_vars(self):
        yaml_text = """
service:
  name: svc
custom_key: custom_value
nested_extra:
  a: 1
  b: 2
"""
        cfg = _parse_yaml_string(yaml_text)
        assert cfg.extra_vars["custom_key"] == "custom_value"
        assert cfg.extra_vars["nested_extra"] == {"a": 1, "b": 2}

    def test_unknown_keys_inside_service_are_ignored(self):
        yaml_text = """
service:
  name: svc
  totally_unknown_field: 42
"""
        cfg = _parse_yaml_string(yaml_text)  # must not raise
        assert cfg.service.name == "svc"

    def test_raises_on_invalid_yaml_syntax(self):
        with pytest.raises(YamlConfigError, match="Invalid YAML syntax"):
            _parse_yaml_string("key: [unclosed bracket")

    def test_raises_on_list_root(self):
        with pytest.raises(YamlConfigError, match="root must be a mapping"):
            _parse_yaml_string("- item1\n- item2\n")

    def test_raises_on_scalar_root(self):
        with pytest.raises(YamlConfigError, match="root must be a mapping"):
            _parse_yaml_string("just a plain string\n")

    def test_raises_on_multi_document(self):
        yaml_text = "key: a\n---\nkey: b\n"
        with pytest.raises(YamlConfigError, match="exactly one document"):
            _parse_yaml_string(yaml_text)

    def test_single_doc_with_trailing_separator_is_ok(self):
        """A trailing ``---`` with no content after should not be treated as a second doc."""
        yaml_text = "version: '3'\n---\n"
        cfg = _parse_yaml_string(yaml_text)
        assert cfg.version == "3"

    def test_raises_on_invalid_port_type(self):
        with pytest.raises(YamlConfigError, match="validation failed"):
            _parse_yaml_string("service:\n  port: not_a_number\n")

    def test_raises_on_port_out_of_range(self):
        with pytest.raises(YamlConfigError, match="validation failed"):
            _parse_yaml_string("service:\n  port: 99999\n")

    def test_raises_on_invalid_log_level(self):
        with pytest.raises(YamlConfigError, match="validation failed"):
            _parse_yaml_string("service:\n  log_level: VERBOSE\n")

    def test_source_path_attached_to_error(self):
        p = Path("/some/fake/config.yaml")
        with pytest.raises(YamlConfigError) as exc_info:
            _parse_yaml_string("- bad\n", source_path=p)
        assert exc_info.value.path == p

    def test_log_level_case_insensitive(self):
        for raw, expected in [("warning", "WARNING"), ("Error", "ERROR"), ("CRITICAL", "CRITICAL")]:
            cfg = _parse_yaml_string(f"service:\n  log_level: {raw}\n")
            assert cfg.service.log_level == expected

    def test_database_defaults_apply_for_partial_section(self):
        cfg = _parse_yaml_string("database:\n  name: partial\n")
        assert cfg.database.host == "localhost"
        assert cfg.database.port == 5432
        assert cfg.database.pool_size == 5


# ---------------------------------------------------------------------------
# parse_yaml_config — integration tests (uses tmp_path filesystem fixture)
# ---------------------------------------------------------------------------


class TestParseYamlConfig:
    """Tests for the public parse_yaml_config API."""

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = parse_yaml_config(tmp_path / "nonexistent.yaml")
        assert isinstance(cfg, YamlConfig)
        assert cfg.version == ""

    def test_loads_valid_yaml_file(self, tmp_path):
        p = _write(
            tmp_path,
            "config.yaml",
            "version: '1.0'\nservice:\n  name: hello\n  port: 7777\n",
        )
        cfg = parse_yaml_config(p)
        assert cfg.version == "1.0"
        assert cfg.service.name == "hello"
        assert cfg.service.port == 7777

    def test_accepts_string_path(self, tmp_path):
        p = _write(tmp_path, "config.yaml", "version: 'str-path'\n")
        cfg = parse_yaml_config(str(p))
        assert cfg.version == "str-path"

    def test_accepts_yml_extension(self, tmp_path):
        p = _write(tmp_path, "config.yml", "version: 'yml'\n")
        cfg = parse_yaml_config(p)
        assert cfg.version == "yml"

    def test_raises_on_parse_error_in_existing_file(self, tmp_path):
        p = _write(tmp_path, "bad.yaml", "key: [broken\n")
        with pytest.raises(YamlConfigError, match="Invalid YAML syntax"):
            parse_yaml_config(p)

    def test_raises_on_unreadable_file(self, tmp_path):
        """Simulate an I/O error by removing read permissions."""
        import os

        p = _write(tmp_path, "noperm.yaml", "version: '1'\n")
        p.chmod(0o000)
        try:
            with pytest.raises(YamlConfigError, match="Cannot read YAML config file"):
                parse_yaml_config(p)
        finally:
            p.chmod(0o644)  # restore so tmp_path cleanup succeeds

    def test_empty_yaml_file_returns_defaults(self, tmp_path):
        p = _write(tmp_path, "empty.yaml", "")
        cfg = parse_yaml_config(p)
        assert cfg == YamlConfig()

    def test_error_carries_path(self, tmp_path):
        p = _write(tmp_path, "bad.yaml", "- list\n")
        with pytest.raises(YamlConfigError) as exc_info:
            parse_yaml_config(p)
        assert exc_info.value.path == p

    def test_full_round_trip(self, tmp_path):
        content = """
version: "42"
service:
  name: round-trip-svc
  port: 1234
  debug: false
  log_level: WARNING
database:
  host: pg.internal
  port: 5433
  name: capdb
  pool_size: 20
  pool_timeout_seconds: 45
feature_flags:
  dark_mode: true
"""
        p = _write(tmp_path, "full.yaml", content)
        cfg = parse_yaml_config(p)

        assert cfg.version == "42"
        assert cfg.service == YamlServiceConfig(
            name="round-trip-svc",
            port=1234,
            debug=False,
            log_level="WARNING",
        )
        assert cfg.database == YamlDatabaseConfig(
            host="pg.internal",
            port=5433,
            name="capdb",
            pool_size=20,
            pool_timeout_seconds=45,
        )
        assert cfg.extra_vars == {"feature_flags": {"dark_mode": True}}


# ---------------------------------------------------------------------------
# YamlConfig model — unit tests
# ---------------------------------------------------------------------------


class TestYamlConfigModel:
    """Direct model construction and equality tests."""

    def test_default_construction(self):
        cfg = YamlConfig()
        assert cfg.version == ""
        assert cfg.service.port == 8080
        assert cfg.database.host == "localhost"
        assert cfg.extra_vars == {}

    def test_two_defaults_are_equal(self):
        assert YamlConfig() == YamlConfig()

    def test_model_validate_from_dict(self):
        data = {"version": "v1", "service": {"port": 9000}}
        cfg = YamlConfig.model_validate(data)
        assert cfg.version == "v1"
        assert cfg.service.port == 9000
        assert cfg.database.host == "localhost"  # default preserved

    def test_service_port_boundary_values(self):
        YamlConfig.model_validate({"service": {"port": 1}})
        YamlConfig.model_validate({"service": {"port": 65535}})

    def test_service_port_zero_is_invalid(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            YamlConfig.model_validate({"service": {"port": 0}})

    def test_database_pool_size_minimum_one(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            YamlConfig.model_validate({"database": {"pool_size": 0}})

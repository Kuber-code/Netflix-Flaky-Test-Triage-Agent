from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from flaketriage.config import (
    API_KEY_ENV_VAR,
    CONFIG_FILENAME,
    Config,
    api_key_from_env,
    find_config_file,
    load_config,
)


def test_defaults_are_valid() -> None:
    config = Config()
    assert config.detect.window_executions > config.detect.min_observations
    assert 0.0 < config.detect.flake_rate_threshold < 1.0
    assert config.classify.temperature == 0.0


def test_tracked_config_file_loads(repo_root: Path) -> None:
    """The committed flaketriage.toml must parse and match the declared schema."""
    config = load_config(repo_root / CONFIG_FILENAME)
    assert config.root == repo_root
    assert config.policy.quarantine_ttl_days == 14
    assert config.classify.budget_usd == pytest.approx(0.50)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """A typo must fail loudly rather than silently keep a default threshold."""
    path = tmp_path / CONFIG_FILENAME
    path.write_text("[detect]\nwindow_executons = 10\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_out_of_range_threshold_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_FILENAME
    path.write_text("[detect]\nflake_rate_threshold = 1.5\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_config_is_immutable() -> None:
    config = Config()
    with pytest.raises(ValidationError):
        config.detect.flake_rate_threshold = 0.9


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_FILENAME
    path.write_text('[store]\npath = "state/store.sqlite"\n', encoding="utf-8")
    config = load_config(path)
    assert config.store_path() == tmp_path / "state" / "store.sqlite"


def test_absolute_store_path_is_left_alone(tmp_path: Path) -> None:
    absolute = (tmp_path / "elsewhere.sqlite").resolve()
    path = tmp_path / CONFIG_FILENAME
    path.write_text(f'[store]\npath = "{absolute.as_posix()}"\n', encoding="utf-8")
    assert load_config(path).store_path() == absolute


def test_find_config_file_searches_ancestors(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text("", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config_file(nested) == tmp_path / CONFIG_FILENAME


def test_missing_config_falls_back_to_defaults(tmp_path: Path) -> None:
    assert load_config(start=tmp_path) == Config()


def test_model_ids_are_env_overridable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKETRIAGE_CLASSIFIER_MODEL", "claude-opus-5")
    config = load_config(start=tmp_path)
    assert config.classify.classifier_model == "claude-opus-5"
    assert config.classify.prefilter_model == Config().classify.prefilter_model


def test_thresholds_are_not_env_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thresholds must be reviewable in version control, not set by env vars."""
    monkeypatch.setenv("FLAKETRIAGE_FLAKE_RATE_THRESHOLD", "0.99")
    config = load_config(start=tmp_path)
    assert config.detect.flake_rate_threshold == Config().detect.flake_rate_threshold


def test_api_key_comes_from_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    assert api_key_from_env() is None
    monkeypatch.setenv(API_KEY_ENV_VAR, "  sk-ant-test  ")
    assert api_key_from_env() == "sk-ant-test"


def test_config_schema_has_no_api_key_field() -> None:
    """A key that cannot be configured cannot be committed by accident."""
    fields = {
        name
        for section in Config().__dict__.values()
        if hasattr(section, "__dict__")
        for name in section.__dict__
    }
    assert not any("key" in name or "token" in name or "secret" in name for name in fields)

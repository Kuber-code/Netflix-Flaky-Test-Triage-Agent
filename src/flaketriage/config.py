"""Configuration loading.

Every behavioural threshold is declared in ``flaketriage.toml`` and validated
here. Two rules are enforced structurally rather than by convention:

1. No magic numbers in code -- if a number influences behaviour it belongs in a
   field on one of these models.
2. No secrets in config -- the Anthropic API key is read from the environment
   only, by :func:`api_key_from_env`. There is deliberately no config field for
   it, so a key cannot be committed by accident.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME: Final = "flaketriage.toml"
API_KEY_ENV_VAR: Final = "ANTHROPIC_API_KEY"

_CLASSIFIER_MODEL_ENV_VAR: Final = "FLAKETRIAGE_CLASSIFIER_MODEL"
_PREFILTER_MODEL_ENV_VAR: Final = "FLAKETRIAGE_PREFILTER_MODEL"


class _Section(BaseModel):
    """Base for config sections: immutable, and unknown keys are an error.

    Rejecting extra keys means a typo in ``flaketriage.toml`` fails loudly at
    startup instead of silently leaving a threshold at its default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class StoreConfig(_Section):
    path: Path = Path(".flaketriage/store.sqlite")


class DetectConfig(_Section):
    window_executions: int = Field(default=50, ge=1)
    min_observations: int = Field(default=10, ge=1)
    flake_rate_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    ewma_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
    regression_pass_streak: int = Field(default=3, ge=1)
    regression_fail_streak: int = Field(default=2, ge=1)


class IdentityConfig(_Section):
    alias_max_distance: float = Field(default=0.25, ge=0.0, le=1.0)
    alias_certain_distance: float = Field(default=0.10, ge=0.0, le=1.0)


class ClassifyConfig(_Section):
    prefilter_model: str = "claude-haiku-4-5-20251001"
    classifier_model: str = "claude-sonnet-5"
    confidence_floor: float = Field(default=0.55, ge=0.0, le=1.0)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    budget_usd: float = Field(default=0.50, ge=0.0)
    max_tests: int = Field(default=25, ge=1)
    cache_path: Path = Path(".flaketriage/cache")
    trace_budget_chars: int = Field(default=2000, ge=200)


class PolicyConfig(_Section):
    quarantine_flake_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    quarantine_min_observations: int = Field(default=10, ge=1)
    quarantine_ttl_days: int = Field(default=14, ge=1)
    dequarantine_clean_runs: int = Field(default=20, ge=1)


class ReportConfig(_Section):
    pr_comment_max_rows: int = Field(default=10, ge=1)


class Config(_Section):
    """The fully resolved configuration for one invocation."""

    store: StoreConfig = StoreConfig()
    detect: DetectConfig = DetectConfig()
    identity: IdentityConfig = IdentityConfig()
    classify: ClassifyConfig = ClassifyConfig()
    policy: PolicyConfig = PolicyConfig()
    report: ReportConfig = ReportConfig()

    # Directory the config was loaded from; relative paths resolve against it so
    # that running the CLI from a subdirectory still finds the same store.
    root: Path = Path()

    def store_path(self) -> Path:
        return self._resolve(self.store.path)

    def cache_path(self) -> Path:
        return self._resolve(self.classify.cache_path)

    def _resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.root / path


def find_config_file(start: Path | None = None) -> Path | None:
    """Search ``start`` and its ancestors for ``flaketriage.toml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        path = candidate / CONFIG_FILENAME
        if path.is_file():
            return path
    return None


def load_config(path: Path | None = None, *, start: Path | None = None) -> Config:
    """Load configuration, falling back to documented defaults.

    Model identifiers may be overridden from the environment so that CI can pin
    a cheaper model without editing tracked files. Nothing else is
    environment-overridable: thresholds must be reviewable in version control.
    """
    config_path = path or find_config_file(start)
    if config_path is None:
        return _apply_env_overrides(Config())

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    raw["root"] = config_path.parent
    return _apply_env_overrides(Config.model_validate(raw))


def _apply_env_overrides(config: Config) -> Config:
    classifier = os.environ.get(_CLASSIFIER_MODEL_ENV_VAR)
    prefilter = os.environ.get(_PREFILTER_MODEL_ENV_VAR)
    if classifier is None and prefilter is None:
        return config
    updates: dict[str, str] = {}
    if classifier:
        updates["classifier_model"] = classifier
    if prefilter:
        updates["prefilter_model"] = prefilter
    return config.model_copy(update={"classify": config.classify.model_copy(update=updates)})


def api_key_from_env() -> str | None:
    """Return the Anthropic API key, or ``None`` if the environment has none.

    Callers must treat ``None`` as "run without the LLM layer" rather than as an
    error: the deterministic core is fully functional without a key.
    """
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    return key or None

"""Assembling a classifier from configuration.

Kept out of the CLI so the eval harness builds its classifier the same way the
CLI does. If the harness constructed its own, the measured numbers would describe
a configuration nobody runs.
"""

from __future__ import annotations

from flaketriage.classify.cache import ClassificationCache
from flaketriage.classify.classifier import Classifier
from flaketriage.classify.client import AnthropicClient, ModelClient
from flaketriage.classify.pricing import cost_table_from_config
from flaketriage.config import Config, api_key_from_env
from flaketriage.obs import get_logger

log = get_logger(__name__)


def build_classifier(
    config: Config,
    *,
    enabled: bool = True,
    budget_usd: float | None = None,
    use_cache: bool = True,
    client: ModelClient | None = None,
) -> Classifier:
    """Build a classifier, or a disabled one when no key is available.

    A missing key is a mode, not an error: the deterministic core is fully
    functional without one, so this returns a classifier that abstains with
    ``LLM_DISABLED`` rather than raising. Callers report the reason to the user.
    """
    resolved: ModelClient | None = client
    if resolved is None and enabled:
        api_key = api_key_from_env()
        if api_key is None:
            log.info("llm_disabled", reason="no_api_key")
        else:
            resolved = AnthropicClient(
                api_key,
                max_retries=config.classify.api_max_retries,
                timeout=config.classify.request_timeout_seconds,
            )

    return Classifier(
        resolved if enabled else None,
        config.classify,
        cache=ClassificationCache(config.cache_path(), enabled=use_cache),
        cost_table=cost_table_from_config(config.classify.prices),
        budget_usd=budget_usd,
    )

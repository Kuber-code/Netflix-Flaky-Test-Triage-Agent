"""LLM classifier: prompts, schema, cache, prefilter, budget.

The only package permitted to talk to a model. Nothing in the deterministic core
imports from here -- see ADR-0001, and the import-graph test that enforces it.
"""

from flaketriage.classify.cache import ClassificationCache, context_key
from flaketriage.classify.classifier import CallRecord, Classifier, ClassifyStats
from flaketriage.classify.client import AnthropicClient, ModelClient, ModelResponse
from flaketriage.classify.pricing import CostTable, cost_table_from_config
from flaketriage.classify.prompt import PROMPT_VERSION, build_context, prompt_version_hash
from flaketriage.classify.schema import (
    Classification,
    DowngradeReason,
    RawClassification,
    abstain,
    json_schema,
    parse_and_validate,
)
from flaketriage.classify.taxonomy import CAUSE_GUIDANCE, CauseCode
from flaketriage.classify.wiring import build_classifier

__all__ = [
    "CAUSE_GUIDANCE",
    "PROMPT_VERSION",
    "AnthropicClient",
    "CallRecord",
    "CauseCode",
    "Classification",
    "ClassificationCache",
    "Classifier",
    "ClassifyStats",
    "CostTable",
    "DowngradeReason",
    "ModelClient",
    "ModelResponse",
    "RawClassification",
    "abstain",
    "build_classifier",
    "build_context",
    "context_key",
    "cost_table_from_config",
    "json_schema",
    "parse_and_validate",
    "prompt_version_hash",
]

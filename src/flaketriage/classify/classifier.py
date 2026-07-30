"""Classification orchestration.

The contract this module exists to keep: **no input, and no model behaviour,
produces an exception.** Every path -- cache hit, prefilter rejection, malformed
JSON, invented cause code, budget exhaustion, API outage -- ends in a
:class:`Classification`, and one carrying an explicit reason when it is an
abstention. A triage tool that raises on a bad model response has moved the
model's unreliability into the build.

Order of operations, and why:

1. **Cache.** Free, and the same trace recurs constantly.
2. **Budget check.** Before the call, not after: a ceiling enforced after
   spending is not a ceiling.
3. **Prefilter.** A cheap model answers "is there enough signal to classify at
   all?"; only positives escalate. Rejections are recorded as ``PREFILTERED``
   abstentions so the eval harness can measure what the gate loses.
4. **Classify, validate, and on a schema failure repair once.** The repair prompt
   is told what was wrong with the previous answer. A second failure abstains and
   logs the malformed text for inspection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from flaketriage.classify.cache import ClassificationCache, context_key
from flaketriage.classify.client import ModelClient, ModelResponse
from flaketriage.classify.pricing import CostTable
from flaketriage.classify.prompt import (
    PREFILTER_SCHEMA,
    PREFILTER_SYSTEM_PROMPT,
    REPAIR_INSTRUCTION,
    build_context,
    build_prefilter_context,
    prompt_version_hash,
    system_prompt,
)
from flaketriage.classify.schema import (
    Classification,
    DowngradeReason,
    abstain,
    json_schema,
    parse_and_validate,
)
from flaketriage.config import ClassifyConfig
from flaketriage.detect.models import Detection
from flaketriage.models import DiffSummary
from flaketriage.obs import get_logger

log = get_logger(__name__)

_MALFORMED_SAMPLE_CHARS: Final = 800


class CallRecord:
    """One model call, logged and aggregated. Feeds `stats` and the eval table."""

    __slots__ = (
        "cache_hit",
        "cost_usd",
        "error",
        "input_tokens",
        "kind",
        "latency_ms",
        "model",
        "output_tokens",
        "schema_valid",
    )

    def __init__(
        self,
        *,
        kind: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        cache_hit: bool = False,
        schema_valid: bool = True,
        error: str | None = None,
    ) -> None:
        self.kind = kind
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.cost_usd = cost_usd
        self.cache_hit = cache_hit
        self.schema_valid = schema_valid
        self.error = error


class ClassifyStats:
    """Run-level accounting, reported rather than merely collected."""

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []
        self.cache_hits = 0
        self.cache_misses = 0
        self.prefiltered = 0
        self.repairs_attempted = 0
        self.repairs_succeeded = 0
        self.schema_failures = 0
        self.budget_exhausted_count = 0

    @property
    def total_cost_usd(self) -> float:
        return sum(record.cost_usd for record in self.calls)

    @property
    def api_calls(self) -> int:
        return len(self.calls)

    @property
    def cache_hit_rate(self) -> float:
        lookups = self.cache_hits + self.cache_misses
        return self.cache_hits / lookups if lookups else 0.0

    def latencies_ms(self, kind: str | None = None) -> list[float]:
        return [record.latency_ms for record in self.calls if kind is None or record.kind == kind]


class Classifier:
    """Classifies detections into causes, with every guardrail applied."""

    def __init__(
        self,
        client: ModelClient | None,
        config: ClassifyConfig,
        *,
        cache: ClassificationCache | None = None,
        cost_table: CostTable | None = None,
        budget_usd: float | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._cache = cache
        self._costs = cost_table or CostTable({})
        self._budget_usd = config.budget_usd if budget_usd is None else budget_usd
        self._prompt_version = prompt_version_hash()
        self.stats = ClassifyStats()

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def budget_remaining_usd(self) -> float:
        return max(self._budget_usd - self.stats.total_cost_usd, 0.0)

    def _cache_model_id(self) -> str:
        """Model identity for the cache key.

        Includes the prefilter model when the gate is enabled, because the gate can
        turn a classifiable failure into a ``PREFILTERED`` abstention -- so two runs
        with different gate models are not interchangeable, even though the
        expensive model is the same.
        """
        if not self._config.prefilter_enabled:
            return self._config.classifier_model
        return f"{self._config.classifier_model}+gate:{self._config.prefilter_model}"

    def _mean_classify_cost(self) -> float:
        """Average cost of a classification call so far, or 0.0 if none yet."""
        billed = [
            record.cost_usd for record in self.stats.calls if record.kind in {"classify", "repair"}
        ]
        return sum(billed) / len(billed) if billed else 0.0

    def _would_exceed_budget(self) -> bool:
        """Whether making one more call would take spend past the ceiling.

        **The guarantee, stated precisely:** total spend is bounded by the
        configured ceiling plus at most one call, because a call's cost is not
        knowable until it returns. Checking only "is anything left?" is weaker
        still -- it permits an unbounded final call at every remaining cent -- so
        the observed mean cost of previous calls is used as the estimate. After
        the first call the cap is effectively exact; the first call is the one
        that cannot be predicted, and that limitation is documented rather than
        papered over with a worst-case estimate that would block small budgets
        entirely.
        """
        spent = self.stats.total_cost_usd
        if spent >= self._budget_usd:
            return True
        estimate = self._mean_classify_cost()
        return estimate > 0.0 and spent + estimate > self._budget_usd

    def classify_many(
        self,
        detections: Sequence[Detection],
        *,
        diff: DiffSummary | None = None,
        max_tests: int | None = None,
    ) -> dict[int, Classification]:
        """Classify a batch, prioritized by flake rate then evidence volume.

        The cap is applied after sorting, so the tests that get the budget are the
        ones most likely to be worth explaining. Tests beyond the cap are returned
        as explicit abstentions rather than omitted -- a missing row reads as "no
        problem here", which is the opposite of the truth.
        """
        limit = self._config.max_tests if max_tests is None else max_tests
        ordered = sorted(detections, key=lambda detection: detection.priority, reverse=True)

        results: dict[int, Classification] = {}
        for index, detection in enumerate(ordered):
            if index >= limit:
                results[detection.identity_id] = abstain(
                    DowngradeReason.BUDGET_EXHAUSTED,
                    prompt_version=self._prompt_version,
                    detail=f"beyond the per-invocation cap of {limit} tests",
                )
                continue
            results[detection.identity_id] = self.classify(detection, diff=diff)

        log.info(
            "classify_batch_complete",
            requested=len(detections),
            classified=self.stats.api_calls,
            cache_hits=self.stats.cache_hits,
            prefiltered=self.stats.prefiltered,
            schema_failures=self.stats.schema_failures,
            cost_usd=round(self.stats.total_cost_usd, 6),
        )
        return results

    def classify(self, detection: Detection, *, diff: DiffSummary | None = None) -> Classification:
        """Classify one detection. Never raises."""
        if self._client is None:
            return abstain(
                DowngradeReason.LLM_DISABLED,
                prompt_version=self._prompt_version,
                detail="classification was not requested",
            )

        context = build_context(
            detection, diff=diff, trace_budget_chars=self._config.trace_budget_chars
        )
        model = self._config.classifier_model
        key = context_key(
            context, model=self._cache_model_id(), prompt_version=self._prompt_version
        )

        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached
            self.stats.cache_misses += 1

        # Checked before spending, not after: a ceiling enforced after the fact
        # is not a ceiling. See _would_exceed_budget for the exact guarantee.
        if self._would_exceed_budget():
            self.stats.budget_exhausted_count += 1
            return abstain(
                DowngradeReason.BUDGET_EXHAUSTED,
                model=model,
                prompt_version=self._prompt_version,
                detail=f"per-invocation budget of ${self._budget_usd:.2f} is exhausted",
            )

        if self._config.prefilter_enabled and not self._prefilter(detection):
            self.stats.prefiltered += 1
            result = abstain(
                DowngradeReason.PREFILTERED,
                model=self._config.prefilter_model,
                prompt_version=self._prompt_version,
                detail="the cheap triage gate found no specific evidence to classify",
            )
            self._store(key, result)
            return result

        result = self._classify_with_repair(context, model)
        self._store(key, result)
        return result

    def _classify_with_repair(self, context: str, model: str) -> Classification:
        response = self._call(kind="classify", model=model, system=system_prompt(), user=context)
        if response.error is not None:
            return abstain(
                DowngradeReason.API_ERROR,
                model=model,
                prompt_version=self._prompt_version,
                detail=f"model call failed: {response.error}",
            )

        classification, malformed = parse_and_validate(
            response.text,
            confidence_floor=self._config.confidence_floor,
            model=model,
            prompt_version=self._prompt_version,
        )
        if malformed is None:
            return classification

        # Schema failure: retry once, telling the model what was wrong.
        self.stats.schema_failures += 1
        self.stats.repairs_attempted += 1
        log.warning(
            "classification_schema_invalid",
            model=model,
            reason=classification.downgrade_reason.value,
            sample=malformed[:_MALFORMED_SAMPLE_CHARS],
        )

        if self._would_exceed_budget():
            return abstain(
                DowngradeReason.BUDGET_EXHAUSTED,
                model=model,
                prompt_version=self._prompt_version,
                detail="schema repair skipped: budget exhausted",
            )

        repair_prompt = (
            f"{context}\n\n"
            f"{REPAIR_INSTRUCTION.format(reason=classification.downgrade_reason.value)}\n\n"
            f"Your previous response was:\n{(response.text or '')[:_MALFORMED_SAMPLE_CHARS]}"
        )
        repaired_response = self._call(
            kind="repair", model=model, system=system_prompt(), user=repair_prompt
        )
        if repaired_response.error is not None:
            return abstain(
                DowngradeReason.API_ERROR,
                model=model,
                prompt_version=self._prompt_version,
                detail=f"repair call failed: {repaired_response.error}",
            )

        repaired, still_malformed = parse_and_validate(
            repaired_response.text,
            confidence_floor=self._config.confidence_floor,
            model=model,
            prompt_version=self._prompt_version,
        )
        if still_malformed is None:
            self.stats.repairs_succeeded += 1
            return repaired

        # Second failure: abstain, and keep the sample. Two malformed responses to
        # one context is a prompt or schema problem worth a human looking at it.
        log.warning(
            "classification_repair_failed",
            model=model,
            sample=still_malformed[:_MALFORMED_SAMPLE_CHARS],
        )
        return abstain(
            DowngradeReason.SCHEMA_INVALID,
            model=model,
            prompt_version=self._prompt_version,
            detail="model output failed schema validation twice",
        )

    def _prefilter(self, detection: Detection) -> bool:
        """Cheap gate. Any doubt, including an API failure, resolves to escalate.

        A wrong rejection silently loses a classification and is invisible in the
        output; a wrong escalation costs one call. The asymmetry decides the
        default.
        """
        response = self._call(
            kind="prefilter",
            model=self._config.prefilter_model,
            system=PREFILTER_SYSTEM_PROMPT,
            user=build_prefilter_context(detection),
            max_tokens=self._config.prefilter_max_output_tokens,
            schema=PREFILTER_SCHEMA,
        )
        if response.error is not None or not response.text:
            return True
        return _read_prefilter_verdict(response.text)

    def _call(
        self,
        *,
        kind: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int | None = None,
        schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """Make one model call and record its cost, latency and outcome."""
        assert self._client is not None  # guarded by classify()

        response = self._client.complete(
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens or self._config.max_output_tokens,
            temperature=self._config.temperature,
            schema=json_schema() if schema is None else schema,
        )
        cost = self._costs.cost_usd(model, response.input_tokens, response.output_tokens)
        self.stats.calls.append(
            CallRecord(
                kind=kind,
                model=model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                cost_usd=cost,
                error=response.error,
            )
        )
        log.info(
            "llm_call",
            kind=kind,
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=round(cost, 6),
            latency_ms=round(response.latency_ms, 1),
            cache_hit=False,
            error=response.error,
            prompt_version=self._prompt_version,
        )
        return response

    def _store(self, key: str, classification: Classification) -> None:
        if self._cache is not None:
            self._cache.put(key, classification)


def _read_prefilter_verdict(text: str) -> bool:
    """Read the gate's answer. Anything unreadable escalates.

    The gate is a cost optimization, so every ambiguity resolves toward spending
    the call: a misparsed rejection loses a classification silently, which is the
    failure mode that does not show up in any metric.
    """
    try:
        payload = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    verdict = payload.get("classifiable")
    return True if not isinstance(verdict, bool) else verdict

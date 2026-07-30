"""A scripted model client.

The behaviours this project claims -- malformed output never raises, budget
exhaustion degrades gracefully, a schema failure is repaired once -- are exactly
the ones a live API will not reproduce on request. So the classifier is tested
against a client that can be told to return anything, including nonsense.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from flaketriage.classify.client import ModelResponse


class FakeClient:
    """Returns queued responses in order; repeats the last one when exhausted."""

    def __init__(
        self,
        responses: Sequence[str | ModelResponse | None],
        *,
        input_tokens: int = 400,
        output_tokens: int = 80,
        latency_ms: float = 25.0,
    ) -> None:
        self._responses = list(responses)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._latency_ms = latency_ms
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None = None,
        schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "schema": schema,
            }
        )
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        queued = self._responses[index] if self._responses else None

        if isinstance(queued, ModelResponse):
            return queued
        return ModelResponse(
            text=queued,
            model=model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            latency_ms=self._latency_ms,
        )


def valid_response(
    cause: str = "RACE_CONDITION",
    *,
    confidence: float = 0.9,
    evidence: Sequence[str] = ("ThreadPoolExecutor frame in trace",),
    abstained: bool = False,
    reasoning: str = "Intermittent assertion on shared state with executor frames.",
    action: str = "Guard the counter with a lock.",
) -> str:
    return json.dumps(
        {
            "cause": cause,
            "confidence": confidence,
            "reasoning": reasoning,
            "evidence": list(evidence),
            "suggested_action": action,
            "abstained": abstained,
        }
    )


def error_response(reason: str = "rate_limited", model: str = "fake") -> ModelResponse:
    return ModelResponse(text=None, model=model, error=reason, latency_ms=5.0)

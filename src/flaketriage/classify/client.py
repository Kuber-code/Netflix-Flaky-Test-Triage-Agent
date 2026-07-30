"""Model client.

Everything that touches the network lives behind :class:`ModelClient`, a protocol
narrow enough that the entire classifier -- schema validation, repair retry,
abstention, caching, budget -- is testable against a fake. That matters more than
usual here: the behaviours this project claims (malformed output never raises,
budget exhaustion degrades gracefully) are precisely the ones a live API will not
reproduce on demand.

Three details were established by calling the API rather than by assuming:

* **``temperature`` is rejected by some models.** ``claude-sonnet-5`` returns a
  400 saying it is deprecated. Since the spec calls for temperature 0, the client
  sends it, and on that specific rejection drops it and retries once, remembering
  the result for the rest of the process. A hardcoded capability table would rot;
  this adapts.
* **The first content block is not necessarily text.** Models that think emit a
  ``ThinkingBlock`` first, so ``content[0].text`` raises ``AttributeError``. Text
  is found by scanning for the first block that has any.
* **Structured output works via ``output_config.format``** with a JSON schema, so
  the ordinary path returns schema-shaped JSON without prompt-level pleading. The
  validation path still assumes it can be given anything.
"""

from __future__ import annotations

import time
from typing import Any, Final, Protocol

from flaketriage.obs import get_logger

log = get_logger(__name__)

_TEMPERATURE_REJECTION_MARKERS: Final = ("temperature", "deprecated")


class ModelResponse:
    """One completed model call, with the accounting the eval harness needs."""

    __slots__ = ("error", "input_tokens", "latency_ms", "model", "output_tokens", "text")

    def __init__(
        self,
        text: str | None,
        *,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = f"error={self.error!r}" if self.error else f"chars={len(self.text or '')}"
        return f"ModelResponse({self.model}, {state}, {self.latency_ms:.0f}ms)"


class ModelClient(Protocol):
    """The whole surface the classifier needs from a model."""

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None = None,
        schema: dict[str, Any] | None = None,
    ) -> ModelResponse: ...


class AnthropicClient:
    """Real client. Converts every API failure into a :class:`ModelResponse`.

    Exceptions are not allowed to escape: an unavailable API must degrade the run
    to ``UNKNOWN`` results, not fail it. A triage tool that breaks the build when
    its own dependency is down is worse than no triage tool.
    """

    def __init__(self, api_key: str, *, max_retries: int = 2, timeout: float = 60.0) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key, max_retries=max_retries, timeout=timeout
        )
        # Models that rejected `temperature`, learned at runtime.
        self._no_temperature: set[str] = set()

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
        send_temperature = temperature is not None and model not in self._no_temperature
        started = time.perf_counter()

        try:
            response = self._call(
                model=model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature if send_temperature else None,
                schema=schema,
            )
        except self._anthropic.BadRequestError as exc:
            if send_temperature and _is_temperature_rejection(str(exc)):
                # Learn the capability and retry without it, once.
                self._no_temperature.add(model)
                log.info("model_rejects_temperature", model=model)
                return self.complete(
                    model=model,
                    system=system,
                    user=user,
                    max_tokens=max_tokens,
                    temperature=None,
                    schema=schema,
                )
            return self._failure(model, started, "bad_request", exc)
        except self._anthropic.RateLimitError as exc:
            return self._failure(model, started, "rate_limited", exc)
        except self._anthropic.APITimeoutError as exc:
            return self._failure(model, started, "timeout", exc)
        except self._anthropic.APIConnectionError as exc:
            return self._failure(model, started, "connection_error", exc)
        except self._anthropic.APIStatusError as exc:
            return self._failure(model, started, f"http_{exc.status_code}", exc)
        except self._anthropic.AnthropicError as exc:  # pragma: no cover - catch-all
            return self._failure(model, started, "sdk_error", exc)

        latency_ms = (time.perf_counter() - started) * 1000
        return ModelResponse(
            text=_first_text_block(response),
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
        )

    def _call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None,
        schema: dict[str, Any] | None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return self._client.messages.create(**kwargs)

    def _failure(self, model: str, started: float, reason: str, exc: Exception) -> ModelResponse:
        latency_ms = (time.perf_counter() - started) * 1000
        log.warning("model_call_failed", model=model, reason=reason, error=str(exc)[:300])
        return ModelResponse(text=None, model=model, latency_ms=latency_ms, error=reason)


def _is_temperature_rejection(message: str) -> bool:
    lowered = message.lower()
    return all(marker in lowered for marker in _TEMPERATURE_REJECTION_MARKERS)


def _first_text_block(response: Any) -> str | None:
    """First content block carrying text.

    A thinking model puts a ``ThinkingBlock`` first, so indexing ``content[0]``
    is a latent AttributeError that only shows up when someone points the config
    at a reasoning model.
    """
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return None

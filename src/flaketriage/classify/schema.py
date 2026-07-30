"""The classifier's output contract.

Schema enforcement is the core guardrail of this project, so the validation path
is written to have no way to raise. Every failure mode -- malformed JSON, a cause
outside the taxonomy, a confidence out of range, a claim with no evidence --
resolves to an ``UNKNOWN`` result carrying the reason. See ADR-0003.

Two guardrails beyond shape validation, both cheap and both effective:

* **Evidence is mandatory for any non-UNKNOWN cause.** A model that names a cause
  but cannot point at anything in the input has pattern-matched on vibes. This
  single rule catches a large share of confident nonsense.
* **A confidence floor.** Anything below it is downgraded rather than reported,
  because a low-confidence label reads to a human exactly like a high-confidence
  one once it is in a table.
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from flaketriage.models import CauseCode, Classification, DowngradeReason

MAX_REASONING_CHARS: Final = 600
MAX_ACTION_CHARS: Final = 400
MAX_EVIDENCE_ITEMS: Final = 6
MAX_EVIDENCE_CHARS: Final = 300


class RawClassification(BaseModel):
    """Exactly what the model is asked to return.

    Deliberately separate from :class:`Classification`: this is the untrusted
    shape, and keeping it distinct means nothing downstream can accidentally
    consume a model response that has not been through the guardrails.

    **Every field is required**, matching :func:`json_schema` exactly. Giving
    these fields defaults would make an incomplete response validate and then get
    quietly downgraded on the evidence rule -- when the more useful outcome is a
    schema failure that triggers one repair attempt. A response missing
    ``evidence`` because the model forgot the key is a different situation from a
    response that considered the question and found nothing to cite.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    cause: CauseCode
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=MAX_REASONING_CHARS)
    evidence: tuple[str, ...]
    suggested_action: str = Field(max_length=MAX_ACTION_CHARS)
    abstained: bool


def abstain(
    reason: DowngradeReason,
    *,
    model: str = "",
    prompt_version: str = "",
    detail: str = "",
) -> Classification:
    """Build the canonical ``UNKNOWN`` result.

    Every path that cannot produce a trustworthy answer ends here, which is what
    makes "never raises" a property of the module rather than of each caller.
    """
    return Classification(
        cause=CauseCode.UNKNOWN,
        confidence=0.0,
        reasoning=detail,
        evidence=(),
        suggested_action="",
        abstained=True,
        downgrade_reason=reason,
        model=model,
        prompt_version=prompt_version,
    )


#: JSON Schema keywords the API's structured-output mode rejects. Discovered by
#: sending them: a schema with ``minimum``/``maximum`` on a number returns
#: "For 'number' type, properties maximum, minimum are not supported". The
#: dialect is a subset of JSON Schema, and this is the concrete reason the
#: Pydantic validation layer is not redundant with it -- the API cannot enforce a
#: numeric range, so the range is enforced after the response arrives.
UNSUPPORTED_SCHEMA_KEYWORDS: Final = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
)


def json_schema() -> dict[str, Any]:
    """The schema handed to the API's structured-output mode.

    Written by hand rather than generated from the Pydantic model, for two
    reasons: the API requires a closed schema with every property listed as
    required, and it accepts only a subset of JSON Schema (see
    :data:`UNSUPPORTED_SCHEMA_KEYWORDS`). A generated schema would carry
    ``minimum``/``maximum`` from the field constraints and be rejected outright.

    Bounds that cannot be expressed structurally are stated in the descriptions
    instead, and enforced by :class:`RawClassification` on the way back.
    """
    return {
        "type": "object",
        "properties": {
            "cause": {
                "type": "string",
                "enum": [code.value for code in CauseCode],
                "description": "The single most likely cause, from the fixed taxonomy.",
            },
            "confidence": {
                "type": "number",
                "description": (
                    "How well the evidence supports the chosen cause, between 0.0 "
                    "and 1.0 inclusive. The range is stated here rather than as "
                    "schema bounds because the API does not accept them; a value "
                    "outside it is rejected on validation."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "At most three sentences explaining the choice.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Concrete observations quoted or referenced from the input. "
                    "Must be non-empty unless cause is UNKNOWN."
                ),
            },
            "suggested_action": {
                "type": "string",
                "description": "At most two sentences of remediation advice.",
            },
            "abstained": {
                "type": "boolean",
                "description": "True when declining to assign a cause.",
            },
        },
        "required": [
            "cause",
            "confidence",
            "reasoning",
            "evidence",
            "suggested_action",
            "abstained",
        ],
        "additionalProperties": False,
    }


def parse_and_validate(
    text: str | None,
    *,
    confidence_floor: float,
    model: str = "",
    prompt_version: str = "",
) -> tuple[Classification, str | None]:
    """Turn raw model output into a trustworthy result. Never raises.

    Returns the classification and, when something was wrong with the output, the
    offending text so the caller can log a sample for inspection.
    """
    if text is None or not text.strip():
        return abstain(
            DowngradeReason.EMPTY_RESPONSE, model=model, prompt_version=prompt_version
        ), text

    try:
        payload = json.loads(_strip_code_fence(text))
    except (json.JSONDecodeError, ValueError):
        return abstain(
            DowngradeReason.INVALID_JSON, model=model, prompt_version=prompt_version
        ), text

    if not isinstance(payload, dict):
        return abstain(
            DowngradeReason.SCHEMA_INVALID, model=model, prompt_version=prompt_version
        ), text

    # An unrecognized cause string is its own failure mode: the model invented a
    # category. Checked before Pydantic so the reason is specific.
    raw_cause = payload.get("cause")
    if isinstance(raw_cause, str) and raw_cause.strip().upper() not in {
        code.value for code in CauseCode
    }:
        return abstain(
            DowngradeReason.UNKNOWN_CAUSE, model=model, prompt_version=prompt_version
        ), text

    try:
        raw = RawClassification.model_validate(payload)
    except ValidationError:
        return abstain(
            DowngradeReason.SCHEMA_INVALID, model=model, prompt_version=prompt_version
        ), text

    return apply_guardrails(
        raw, confidence_floor=confidence_floor, model=model, prompt_version=prompt_version
    ), None


def apply_guardrails(
    raw: RawClassification,
    *,
    confidence_floor: float,
    model: str = "",
    prompt_version: str = "",
) -> Classification:
    """Downgrade a structurally valid answer that is not trustworthy."""
    evidence = _clean_evidence(raw.evidence)

    if raw.abstained or raw.cause is CauseCode.UNKNOWN:
        return Classification(
            cause=CauseCode.UNKNOWN,
            confidence=raw.confidence,
            reasoning=raw.reasoning,
            evidence=evidence,
            suggested_action=raw.suggested_action,
            abstained=True,
            downgrade_reason=DowngradeReason.MODEL_ABSTAINED
            if raw.abstained
            else DowngradeReason.NONE,
            model=model,
            prompt_version=prompt_version,
        )

    # A cause with nothing to point at is a guess wearing a label.
    if not evidence:
        return abstain(
            DowngradeReason.NO_EVIDENCE,
            model=model,
            prompt_version=prompt_version,
            detail=raw.reasoning,
        )

    if raw.confidence < confidence_floor:
        return abstain(
            DowngradeReason.BELOW_CONFIDENCE_FLOOR,
            model=model,
            prompt_version=prompt_version,
            detail=raw.reasoning,
        )

    return Classification(
        cause=raw.cause,
        confidence=raw.confidence,
        reasoning=raw.reasoning,
        evidence=evidence,
        suggested_action=raw.suggested_action,
        abstained=False,
        downgrade_reason=DowngradeReason.NONE,
        model=model,
        prompt_version=prompt_version,
    )


def _clean_evidence(items: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = []
    for item in items:
        text = item.strip()
        if text:
            cleaned.append(text[:MAX_EVIDENCE_CHARS])
        if len(cleaned) >= MAX_EVIDENCE_ITEMS:
            break
    return tuple(cleaned)


def _strip_code_fence(text: str) -> str:
    """Tolerate a fenced code block around the JSON.

    Structured-output mode does not produce fences, but the repair prompt and any
    fallback path might, and refusing an otherwise perfect answer over three
    backticks would be pedantry rather than rigour.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    body = lines[1:-1] if len(lines) > 2 and lines[-1].strip().startswith("```") else lines[1:]
    return "\n".join(body).strip()


__all__ = [
    "Classification",
    "DowngradeReason",
    "RawClassification",
    "abstain",
    "apply_guardrails",
    "json_schema",
    "parse_and_validate",
]

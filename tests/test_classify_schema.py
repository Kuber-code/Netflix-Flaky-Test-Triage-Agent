"""Schema validation and the abstention guardrails.

The claim under test is absolute: no model output, however malformed, produces an
exception. The parametrized garbage case is the important one -- it is the answer
to "what happens when the model returns garbage?"
"""

from __future__ import annotations

import json

import pytest

from flaketriage.classify.schema import (
    UNSUPPORTED_SCHEMA_KEYWORDS,
    Classification,
    DowngradeReason,
    RawClassification,
    abstain,
    apply_guardrails,
    json_schema,
    parse_and_validate,
)
from flaketriage.classify.taxonomy import CauseCode

FLOOR = 0.55


def parse(text: str | None, floor: float = FLOOR) -> tuple[Classification, str | None]:
    return parse_and_validate(text, confidence_floor=floor, model="m", prompt_version="v")


def valid_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "cause": "RACE_CONDITION",
        "confidence": 0.9,
        "reasoning": "Shared counter asserted from two threads.",
        "evidence": ["ThreadPoolExecutor frame at line 88"],
        "suggested_action": "Lock the counter.",
        "abstained": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- the happy path --------------------------------------------------------


def test_a_well_formed_response_is_accepted() -> None:
    classification, malformed = parse(valid_payload())
    assert malformed is None
    assert classification.cause is CauseCode.RACE_CONDITION
    assert classification.confidence == 0.9
    assert classification.evidence == ("ThreadPoolExecutor frame at line 88",)
    assert classification.abstained is False
    assert classification.downgrade_reason is DowngradeReason.NONE
    assert classification.is_actionable is True


def test_model_and_prompt_version_are_recorded_for_reproducibility() -> None:
    classification, _ = parse(valid_payload())
    assert classification.model == "m"
    assert classification.prompt_version == "v"


# --- malformed output ------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "   ",
        "not json at all",
        "{",
        "{'cause': 'RACE_CONDITION'}",  # single quotes
        "[]",
        "[1, 2, 3]",
        "null",
        "42",
        '"a string"',
        "<html>502 Bad Gateway</html>",
        '{"cause": "RACE_CONDITION"}',  # missing required fields
        '{"cause": null, "confidence": 0.9}',
        '{"cause": "RACE_CONDITION", "confidence": "very"}',
        '{"cause": "RACE_CONDITION", "confidence": 5.0}',
        '{"cause": "RACE_CONDITION", "confidence": -1}',
        '{"cause": "GREMLINS", "confidence": 0.9}',
        '{"cause": "", "confidence": 0.9}',
        "```json\n{broken\n```",
        "\x00\x01\x02",
    ],
)
def test_garbage_never_raises_and_always_abstains(garbage: str) -> None:
    """The answer to "what happens when the model returns garbage?"."""
    classification, malformed = parse(garbage)
    assert classification.cause is CauseCode.UNKNOWN
    assert classification.abstained is True
    assert classification.downgrade_reason is not DowngradeReason.NONE
    assert malformed == garbage


def test_none_response_abstains() -> None:
    classification, _ = parse(None)
    assert classification.downgrade_reason is DowngradeReason.EMPTY_RESPONSE


def test_an_invented_cause_code_is_its_own_failure_mode() -> None:
    classification, malformed = parse(valid_payload(cause="DEFINITELY_A_RACE"))
    assert classification.downgrade_reason is DowngradeReason.UNKNOWN_CAUSE
    assert malformed is not None


def test_invalid_json_is_distinguished_from_an_invalid_shape() -> None:
    invalid_json, _ = parse("{not json")
    assert invalid_json.downgrade_reason is DowngradeReason.INVALID_JSON

    invalid_shape, _ = parse('{"cause": "RACE_CONDITION", "confidence": 0.9}')
    assert invalid_shape.downgrade_reason is DowngradeReason.SCHEMA_INVALID


def test_a_fenced_code_block_is_tolerated() -> None:
    """Refusing a perfect answer over three backticks would be pedantry."""
    fenced = f"```json\n{valid_payload()}\n```"
    classification, malformed = parse(fenced)
    assert malformed is None
    assert classification.cause is CauseCode.RACE_CONDITION


def test_unexpected_extra_keys_are_ignored_not_fatal() -> None:
    classification, malformed = parse(valid_payload(sentiment="confident"))
    assert malformed is None
    assert classification.cause is CauseCode.RACE_CONDITION


# --- the guardrails --------------------------------------------------------


def test_a_cause_with_no_evidence_is_downgraded() -> None:
    """A model that names a cause but cannot point at anything has guessed."""
    classification, malformed = parse(valid_payload(evidence=[]))
    assert malformed is None
    assert classification.cause is CauseCode.UNKNOWN
    assert classification.downgrade_reason is DowngradeReason.NO_EVIDENCE


def test_whitespace_evidence_does_not_count_as_evidence() -> None:
    classification, _ = parse(valid_payload(evidence=["", "   ", "\n"]))
    assert classification.downgrade_reason is DowngradeReason.NO_EVIDENCE


def test_confidence_below_the_floor_is_downgraded() -> None:
    classification, _ = parse(valid_payload(confidence=0.4))
    assert classification.cause is CauseCode.UNKNOWN
    assert classification.downgrade_reason is DowngradeReason.BELOW_CONFIDENCE_FLOOR


def test_the_confidence_floor_comes_from_config() -> None:
    at_floor, _ = parse(valid_payload(confidence=0.6), 0.55)
    assert at_floor.cause is CauseCode.RACE_CONDITION

    strict, _ = parse(valid_payload(confidence=0.6), 0.9)
    assert strict.cause is CauseCode.UNKNOWN


def test_confidence_exactly_at_the_floor_is_accepted() -> None:
    classification, _ = parse(valid_payload(confidence=FLOOR), FLOOR)
    assert classification.cause is CauseCode.RACE_CONDITION


def test_an_explicit_abstention_is_honoured_and_labelled() -> None:
    classification, malformed = parse(valid_payload(cause="RACE_CONDITION", abstained=True))
    assert malformed is None
    assert classification.cause is CauseCode.UNKNOWN
    assert classification.downgrade_reason is DowngradeReason.MODEL_ABSTAINED


def test_unknown_with_no_evidence_is_fine() -> None:
    """UNKNOWN is a correct answer, so it is not held to the evidence rule."""
    classification, malformed = parse(valid_payload(cause="UNKNOWN", evidence=[], confidence=0.2))
    assert malformed is None
    assert classification.cause is CauseCode.UNKNOWN
    assert classification.is_abstention is True


def test_evidence_is_capped_and_trimmed() -> None:
    classification, _ = parse(valid_payload(evidence=[f"item {index}" for index in range(20)]))
    assert len(classification.evidence) == 6


def test_overlong_evidence_items_are_truncated() -> None:
    classification, _ = parse(valid_payload(evidence=["x" * 5000]))
    assert len(classification.evidence[0]) == 300


def test_overlong_reasoning_is_rejected_rather_than_silently_kept() -> None:
    classification, malformed = parse(valid_payload(reasoning="y" * 5000))
    assert malformed is not None
    assert classification.downgrade_reason is DowngradeReason.SCHEMA_INVALID


# --- abstain() and the emitted schema --------------------------------------


def test_abstain_produces_a_consistent_result() -> None:
    result = abstain(DowngradeReason.BUDGET_EXHAUSTED, model="m", prompt_version="v")
    assert result.cause is CauseCode.UNKNOWN
    assert result.abstained is True
    assert result.confidence == 0.0
    assert result.evidence == ()
    assert result.is_actionable is False


def test_the_emitted_schema_is_closed_and_fully_required() -> None:
    """A schema with optional fields lets the model omit the ones that matter."""
    schema = json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_the_schema_enum_matches_the_taxonomy_exactly() -> None:
    enum = json_schema()["properties"]["cause"]["enum"]
    assert enum == [code.value for code in CauseCode]
    assert "UNKNOWN" in enum


def test_guardrails_can_be_applied_to_a_raw_model_directly() -> None:
    raw = RawClassification(
        cause=CauseCode.EXTERNAL_DEPENDENCY,
        confidence=0.8,
        reasoning="Connection refused reaching the payments stub.",
        evidence=("connection refused",),
        suggested_action="Stub the dependency.",
        abstained=False,
    )
    assert apply_guardrails(raw, confidence_floor=FLOOR).cause is CauseCode.EXTERNAL_DEPENDENCY


def test_the_two_schemas_agree_on_what_is_required() -> None:
    """The emitted JSON schema and the parsing model must not drift apart.

    If the model may omit a field the parser demands, every response is a schema
    failure; if the parser defaults a field the schema demands, an incomplete
    response is silently accepted.
    """
    emitted = set(json_schema()["required"])
    parsed_required = {
        name for name, field in RawClassification.model_fields.items() if field.is_required()
    }
    assert emitted == parsed_required


def test_a_response_missing_a_field_is_a_schema_failure_not_a_downgrade() -> None:
    """A forgotten key is repairable; nothing-to-cite is not the same thing."""
    classification, malformed = parse('{"cause": "RACE_CONDITION", "confidence": 0.9}')
    assert classification.downgrade_reason is DowngradeReason.SCHEMA_INVALID
    assert malformed is not None


# --- taxonomy semantics ----------------------------------------------------


def test_regression_infra_and_unknown_are_not_flake_categories() -> None:
    """The policy engine depends on this to never quarantine a regression."""
    assert CauseCode.REAL_REGRESSION.is_flake_category is False
    assert CauseCode.INFRA_FLAKE.is_flake_category is False
    assert CauseCode.UNKNOWN.is_flake_category is False
    assert CauseCode.RACE_CONDITION.is_flake_category is True


def test_a_regression_classification_is_never_actionable_as_a_flake() -> None:
    classification, _ = parse(valid_payload(cause="REAL_REGRESSION"))
    assert classification.cause is CauseCode.REAL_REGRESSION
    assert classification.is_actionable is False


def test_infra_flake_is_never_actionable_as_a_flake() -> None:
    classification, _ = parse(valid_payload(cause="INFRA_FLAKE"))
    assert classification.is_actionable is False


def test_the_emitted_schema_avoids_keywords_the_api_rejects() -> None:
    """The structured-output dialect is a subset of JSON Schema.

    Sending `minimum`/`maximum` on a number returns a 400. This test exists
    because that failure only appears against the live API, and a generated
    schema would reintroduce the keywords from the field constraints.
    """

    def walk(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in UNSUPPORTED_SCHEMA_KEYWORDS:
                    found.append(key)
                found.extend(walk(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(walk(item))
        return found

    assert walk(json_schema()) == []


def test_the_confidence_range_is_still_enforced_after_the_response() -> None:
    """The API cannot enforce the bound, so validation must."""
    assert "0.0" in json_schema()["properties"]["confidence"]["description"]
    too_high, malformed = parse(valid_payload(confidence=1.5))
    assert malformed is not None
    assert too_high.downgrade_reason is DowngradeReason.SCHEMA_INVALID

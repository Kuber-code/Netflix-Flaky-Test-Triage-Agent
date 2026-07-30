"""Policy engine: quarantine rules, TTL, ownership, de-quarantine.

The most important test here is
``test_a_regression_is_never_recommended_for_quarantine`` -- an acceptance
criterion, and the error with the highest cost: quarantining a real defect
suppresses a signal somebody needs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from flaketriage.config import PolicyConfig
from flaketriage.detect.detector import detect_for_history
from flaketriage.detect.models import Detection, Verdict
from flaketriage.identity.fingerprint import fingerprint
from flaketriage.models import CauseCode, Classification, DowngradeReason, TestIdentity
from flaketriage.policy import (
    CodeOwners,
    OwnerSource,
    QuarantineRecommendation,
    QuarantineState,
    QuarantineStore,
    RefusalReason,
    consecutive_clean_runs,
    evaluate,
    is_expired,
    is_expiring,
    resolve_owner,
    should_release,
    summarize_states,
)
from flaketriage.store.db import IN_MEMORY
from flaketriage.store.repositories import RunStore
from helpers import FAIL, MIXED, PASS, history_from

CONFIG = PolicyConfig()
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

IDENTITY = TestIdentity(
    fingerprint=fingerprint("tests/test_auth.py", "test_login"),
    suite_path="tests/test_auth.py",
    test_name="test_login",
    file_path="tests/test_auth.py",
)


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def flaky_detection(*, observations: int = 12, rate: float = 0.30) -> Detection:
    """A test the detector calls flaky, with a rate over the threshold."""
    history = history_from([("a", MIXED), ("b", MIXED), ("c", PASS)])
    detection = detect_for_history(IDENTITY, 1, history)
    assert detection.verdict is Verdict.FLAKY
    return detection.model_copy(update={"observations": observations, "flake_rate": rate})


def classification(cause: CauseCode) -> Classification:
    return Classification(
        cause=cause,
        confidence=0.9,
        evidence=("frame",),
        abstained=cause is CauseCode.UNKNOWN,
        downgrade_reason=DowngradeReason.NONE,
    )


def decide(
    detection: Detection,
    *,
    classification: Classification | None = None,
    already_quarantined: bool = False,
    codeowners: CodeOwners | None = None,
) -> QuarantineRecommendation:
    """Evaluate with git disabled and a fixed clock, so results are stable."""
    return evaluate(
        detection,
        classification=classification,
        already_quarantined=already_quarantined,
        codeowners=codeowners,
        config=CONFIG,
        allow_git=False,
        now=NOW,
    )


# --- the four conditions ---------------------------------------------------


def test_a_flaky_test_over_threshold_is_recommended() -> None:
    result = decide(flaky_detection())
    assert result.recommended is True
    assert result.refusal is RefusalReason.NONE


def test_a_flake_rate_at_or_below_threshold_is_refused() -> None:
    result = decide(flaky_detection(rate=CONFIG.quarantine_flake_rate))
    assert result.recommended is False
    assert result.refusal is RefusalReason.FLAKE_RATE_BELOW_THRESHOLD


def test_too_few_observations_is_refused() -> None:
    """A high rate over four runs is not evidence of instability."""
    result = decide(flaky_detection(observations=4))
    assert result.recommended is False
    assert result.refusal is RefusalReason.INSUFFICIENT_OBSERVATIONS


def test_an_already_quarantined_test_is_not_recommended_again() -> None:
    result = decide(flaky_detection(), already_quarantined=True)
    assert result.recommended is False
    assert result.refusal is RefusalReason.ALREADY_QUARANTINED


def test_a_healthy_test_is_not_recommended() -> None:
    healthy = detect_for_history(IDENTITY, 1, history_from([("a", PASS), ("b", PASS)]))
    result = decide(healthy)
    assert result.recommended is False
    assert result.refusal is RefusalReason.NOT_FLAKY


# --- the rules that protect real signal ------------------------------------


def test_a_regression_is_never_recommended_for_quarantine() -> None:
    """Acceptance criterion. Telling an engineer to ignore a real bug is the
    most expensive error this tool can make."""
    regression = detect_for_history(
        IDENTITY,
        1,
        history_from([("a", PASS), ("b", PASS), ("c", PASS), ("d", FAIL), ("e", FAIL)]),
    )
    assert regression.verdict is Verdict.REGRESSION

    result = decide(regression.model_copy(update={"observations": 50, "flake_rate": 0.99}))
    assert result.recommended is False
    assert result.refusal is RefusalReason.CAUSE_IS_REGRESSION


def test_a_model_saying_regression_also_vetoes_a_quarantine() -> None:
    """Checked against both layers, because they can disagree.

    Either one saying "regression" is sufficient to refuse -- the detector may not
    have enough history to see the transition that the diff makes obvious.
    """
    result = decide(flaky_detection(), classification=classification(CauseCode.REAL_REGRESSION))
    assert result.recommended is False
    assert result.refusal is RefusalReason.CAUSE_IS_REGRESSION


def test_an_infra_flake_is_never_quarantined() -> None:
    """Blaming a test author for a platform outage destroys trust in the tool."""
    result = decide(flaky_detection(), classification=classification(CauseCode.INFRA_FLAKE))
    assert result.recommended is False
    assert result.refusal is RefusalReason.CAUSE_IS_INFRA


def test_a_new_failure_is_never_quarantined() -> None:
    """No history means no evidence, and quarantine needs evidence."""
    new = detect_for_history(IDENTITY, 1, history_from([("a", FAIL)]))
    assert new.verdict is Verdict.NEW_FAILURE
    result = decide(new.model_copy(update={"observations": 30, "flake_rate": 0.9}))
    assert result.recommended is False


def test_the_model_can_only_veto_never_cause_a_quarantine() -> None:
    """A model that hallucinated a cause on every test could quarantine nothing.

    The classification is one field in a conjunction; the flake rate and the
    observation count still have to be met independently.
    """
    healthy = detect_for_history(IDENTITY, 1, history_from([("a", PASS), ("b", PASS)]))
    result = decide(healthy, classification=classification(CauseCode.RACE_CONDITION))
    assert result.recommended is False

    thin = flaky_detection(observations=2)
    thin_result = decide(thin, classification=classification(CauseCode.RACE_CONDITION))
    assert thin_result.recommended is False


def test_an_abstention_does_not_block_a_quarantine() -> None:
    """UNKNOWN is not a veto: the deterministic evidence stands on its own."""
    result = decide(flaky_detection(), classification=classification(CauseCode.UNKNOWN))
    assert result.recommended is True


# --- TTL, owner, de-quarantine ---------------------------------------------


def test_every_recommendation_carries_a_ttl_and_a_release_condition() -> None:
    """Acceptance criterion: a quarantine without an expiry is a graveyard."""
    result = decide(flaky_detection())
    assert result.ttl_days == CONFIG.quarantine_ttl_days
    assert result.clean_runs_required == CONFIG.dequarantine_clean_runs
    expected = (NOW + timedelta(days=CONFIG.quarantine_ttl_days)).isoformat()
    assert result.expires_at == expected


def test_a_refusal_carries_no_ttl() -> None:
    result = decide(flaky_detection(observations=2))
    assert result.ttl_days == 0
    assert result.expires_at == ""


def test_ttl_comes_from_config() -> None:
    result = evaluate(
        flaky_detection(),
        config=PolicyConfig(quarantine_ttl_days=3),
        allow_git=False,
        now=NOW,
    )
    assert result.ttl_days == 3
    assert result.expires_at.startswith("2026-07-23")


def test_an_owner_from_codeowners_is_marked_authoritative() -> None:
    owners = CodeOwners.parse("tests/ @qa-team\n")
    result = decide(flaky_detection(), codeowners=owners)
    assert result.owner == "@qa-team"
    assert result.owner_source is OwnerSource.CODEOWNERS
    assert result.owner_is_a_guess is False


def test_an_unresolvable_owner_is_reported_not_invented() -> None:
    result = decide(flaky_detection(), codeowners=CodeOwners.parse("docs/ @writers\n"))
    assert result.owner == ""
    assert result.owner_source is OwnerSource.UNRESOLVED


def test_consecutive_clean_runs_stops_at_the_first_failure() -> None:
    assert consecutive_clean_runs([True, True, False, True]) == 2
    assert consecutive_clean_runs([False, True, True]) == 0
    assert consecutive_clean_runs([True, True, True]) == 3
    assert consecutive_clean_runs([]) == 0


def test_release_requires_the_configured_number_of_clean_runs() -> None:
    assert should_release(CONFIG.dequarantine_clean_runs - 1, CONFIG) is False
    assert should_release(CONFIG.dequarantine_clean_runs, CONFIG) is True
    assert should_release(3, PolicyConfig(dequarantine_clean_runs=3)) is True


@pytest.mark.parametrize(
    ("offset_days", "expiring_soon", "expired"),
    [(-1, True, True), (0, True, True), (1, True, False), (10, False, False)],
)
def test_expiry_predicates(offset_days: int, expiring_soon: bool, expired: bool) -> None:
    moment = (NOW + timedelta(days=offset_days)).isoformat()
    assert is_expiring(moment, within_days=3, now=NOW) is expiring_soon
    assert is_expired(moment, now=NOW) is expired


def test_a_malformed_expiry_is_not_treated_as_expired() -> None:
    """Better to leave a quarantine open than to close it on unparseable data."""
    assert is_expired("not a date") is False
    assert is_expiring("not a date") is False


# --- CODEOWNERS parsing ----------------------------------------------------


def test_the_last_matching_codeowners_rule_wins() -> None:
    """GitHub's own precedence, so the file means what its author expects."""
    owners = CodeOwners.parse("* @everyone\ntests/ @qa-team\n")
    assert owners.owners_for("tests/test_a.py") == ("@qa-team",)
    assert owners.owners_for("src/main.py") == ("@everyone",)


def test_codeowners_comments_and_blank_lines_are_ignored() -> None:
    owners = CodeOwners.parse("# owners\n\ntests/ @qa  # the qa team\n")
    assert owners.owners_for("tests/a.py") == ("@qa",)


def test_multiple_owners_are_all_recorded() -> None:
    owners = CodeOwners.parse("tests/ @qa @platform\n")
    assert owners.owners_for("tests/a.py") == ("@qa", "@platform")


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*", "anything/at/all.py", True),
        ("/tests/", "tests/a.py", True),
        ("/tests/", "src/tests/a.py", False),
        ("tests/", "src/tests/a.py", True),
        ("tests/unit/test_a.py", "tests/unit/test_a.py", True),
        ("*.py", "tests/a.py", True),
        ("tests/*.py", "tests/a.py", True),
        ("tests/*.py", "tests/unit/a.py", False),
        ("tests/**/*.py", "tests/unit/deep/a.py", True),
    ],
)
def test_codeowners_pattern_matching(pattern: str, path: str, matches: bool) -> None:
    owners = CodeOwners.parse(f"{pattern} @team\n")
    assert bool(owners.owners_for(path)) is matches


def test_a_single_star_does_not_cross_directories() -> None:
    """fnmatch would match here, which would make tests/*.py own everything."""
    owners = CodeOwners.parse("tests/*.py @qa\n")
    assert owners.owners_for("tests/unit/deep/a.py") == ()


def test_codeowners_is_loaded_from_the_documented_locations(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @primary\n", encoding="utf-8")
    (tmp_path / "CODEOWNERS").write_text("* @secondary\n", encoding="utf-8")
    loaded = CodeOwners.load(tmp_path)
    assert loaded is not None
    assert loaded.owners_for("a.py") == ("@primary",)


def test_no_codeowners_file_is_not_an_error(tmp_path: Path) -> None:
    assert CodeOwners.load(tmp_path) is None


def test_resolve_owner_without_a_path_is_unresolved() -> None:
    assert resolve_owner(None, allow_git=False).source is OwnerSource.UNRESOLVED


# --- persistence and lifecycle --------------------------------------------


def seed(store: RunStore, name: str = "test_login") -> int:
    identity_id, _ = store.upsert_identity(
        TestIdentity(
            fingerprint=f"fp-{name}",
            suite_path="tests/test_auth.py",
            test_name=name,
            file_path="tests/test_auth.py",
        )
    )
    return identity_id


def test_a_recommendation_is_persisted_and_listed(store: RunStore) -> None:
    identity_id = seed(store)
    recommendation = decide(flaky_detection().model_copy(update={"identity_id": identity_id}))
    assert QuarantineStore(store.connection).record([recommendation]) == 1

    (record,) = QuarantineStore(store.connection).records()
    assert record.state is QuarantineState.RECOMMENDED
    assert record.identity_id == identity_id
    assert record.expires_at == recommendation.expires_at
    assert record.clean_runs_required == CONFIG.dequarantine_clean_runs


def test_recording_the_same_recommendation_twice_is_a_no_op(store: RunStore) -> None:
    """A retried CI job re-derives the same recommendation from the same history."""
    identity_id = seed(store)
    recommendation = decide(flaky_detection().model_copy(update={"identity_id": identity_id}))
    assert QuarantineStore(store.connection).record([recommendation]) == 1
    assert QuarantineStore(store.connection).record([recommendation]) == 0
    assert len(QuarantineStore(store.connection).records()) == 1


def test_refusals_are_never_persisted(store: RunStore) -> None:
    identity_id = seed(store)
    refused = decide(
        flaky_detection(observations=2).model_copy(update={"identity_id": identity_id})
    )
    assert QuarantineStore(store.connection).record([refused]) == 0
    assert QuarantineStore(store.connection).records() == []


def test_open_quarantine_ids_feed_the_already_quarantined_check(store: RunStore) -> None:
    identity_id = seed(store)
    QuarantineStore(store.connection).record(
        [decide(flaky_detection().model_copy(update={"identity_id": identity_id}))]
    )
    assert QuarantineStore(store.connection).open_ids() == frozenset({identity_id})


def test_an_expired_quarantine_is_closed_and_reported(store: RunStore) -> None:
    """Expired means the TTL ran out while the test was still unstable.

    Distinct from released, which is the system working -- collapsing them would
    hide the only distinction that matters to a reader.
    """
    identity_id = seed(store)
    stale = decide(flaky_detection().model_copy(update={"identity_id": identity_id}))
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    QuarantineStore(store.connection).record([stale.model_copy(update={"expires_at": past})])

    expired = QuarantineStore(store.connection).expire_overdue()
    assert len(expired) == 1
    assert QuarantineStore(store.connection).records() == []
    (closed,) = QuarantineStore(store.connection).records(open_only=False)
    assert closed.state is QuarantineState.EXPIRED
    assert closed.close_reason is not None
    assert "ttl" in closed.close_reason


def test_a_released_quarantine_records_why(store: RunStore) -> None:
    identity_id = seed(store)
    QuarantineStore(store.connection).record(
        [decide(flaky_detection().model_copy(update={"identity_id": identity_id}))]
    )
    (record,) = QuarantineStore(store.connection).records()

    assert QuarantineStore(store.connection).release(record.record_id, clean_runs=20) is True
    assert QuarantineStore(store.connection).records() == []
    (closed,) = QuarantineStore(store.connection).records(open_only=False)
    assert closed.state is QuarantineState.RELEASED
    assert closed.close_reason == "20 consecutive clean execution(s)"


def test_closing_an_already_closed_quarantine_is_a_no_op(store: RunStore) -> None:
    identity_id = seed(store)
    QuarantineStore(store.connection).record(
        [decide(flaky_detection().model_copy(update={"identity_id": identity_id}))]
    )
    (record,) = QuarantineStore(store.connection).records()
    assert QuarantineStore(store.connection).release(record.record_id, clean_runs=20) is True
    assert QuarantineStore(store.connection).release(record.record_id, clean_runs=20) is False


def test_a_closed_quarantine_frees_the_test_to_be_recommended_again(store: RunStore) -> None:
    identity_id = seed(store)
    recommendation = decide(flaky_detection().model_copy(update={"identity_id": identity_id}))
    QuarantineStore(store.connection).record([recommendation])
    (record,) = QuarantineStore(store.connection).records()
    QuarantineStore(store.connection).release(record.record_id, clean_runs=20)

    assert QuarantineStore(store.connection).open_ids() == frozenset()
    assert QuarantineStore(store.connection).record([recommendation]) == 1


def test_state_counts_are_summarizable(store: RunStore) -> None:
    identity_id = seed(store)
    QuarantineStore(store.connection).record(
        [decide(flaky_detection().model_copy(update={"identity_id": identity_id}))]
    )
    assert summarize_states(QuarantineStore(store.connection).records()) == {"recommended": 1}


def test_recent_outcomes_feeds_the_release_check(store: RunStore) -> None:
    """The de-quarantine path end to end: clean runs accumulate and release."""
    from datetime import timedelta as delta

    from flaketriage.models import Outcome, RunMetadata, TestCaseResult

    identity_id = seed(store)
    base = datetime(2026, 7, 20, tzinfo=UTC)
    for index, outcome in enumerate([Outcome.FAIL, *([Outcome.PASS] * 20)]):
        run_pk = store.record_run(
            RunMetadata(
                commit_sha=f"sha{index}",
                run_id=f"run-{index}",
                started_at=base + delta(minutes=index),
            )
        )
        store.record_executions(
            run_pk, [(identity_id, TestCaseResult(name="test_login", outcome=outcome))]
        )

    outcomes = store.recent_outcomes(identity_id)
    assert outcomes[0] is True  # newest first
    assert consecutive_clean_runs(outcomes) == 20
    assert should_release(consecutive_clean_runs(outcomes), CONFIG) is True


def test_a_skip_does_not_count_toward_release(store: RunStore) -> None:
    """A test that did not run has not earned its way out of quarantine."""
    from datetime import timedelta as delta

    from flaketriage.models import Outcome, RunMetadata, TestCaseResult

    identity_id = seed(store)
    base = datetime(2026, 7, 20, tzinfo=UTC)
    for index, outcome in enumerate([Outcome.FAIL, Outcome.SKIP, Outcome.SKIP]):
        run_pk = store.record_run(
            RunMetadata(
                commit_sha=f"sha{index}",
                run_id=f"run-{index}",
                started_at=base + delta(minutes=index),
            )
        )
        store.record_executions(
            run_pk, [(identity_id, TestCaseResult(name="test_login", outcome=outcome))]
        )

    # The skips are omitted entirely, so the trailing history is the failure.
    assert store.recent_outcomes(identity_id) == [False]
    assert consecutive_clean_runs(store.recent_outcomes(identity_id)) == 0

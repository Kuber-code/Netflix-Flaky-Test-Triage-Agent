"""Rename reconciliation against the store, including the P2 property test.

The property that matters is stated in the spec as the phase's exit criterion:
renaming a test preserves its history. If that fails, every flake rate resets
the moment an engineer touches a flaky test's name -- exactly when the history
is most needed.
"""

from __future__ import annotations

import string
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flaketriage.identity.fingerprint import identity_for
from flaketriage.identity.reconcile import reconcile_renames
from flaketriage.models import Outcome, RunMetadata, TestCaseResult
from flaketriage.store.db import IN_MEMORY
from flaketriage.store.repositories import RunStore

BASE_TIME = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def record_run(
    store: RunStore,
    index: int,
    cases: list[TestCaseResult],
    *,
    sha: str | None = None,
) -> int:
    """Persist one run's worth of cases and reconcile renames, as ingest does."""
    metadata = RunMetadata(
        commit_sha=sha or f"sha{index:04d}",
        run_id=f"run-{index}",
        started_at=BASE_TIME + timedelta(minutes=index),
    )
    run_pk = store.record_run(metadata)
    rows = []
    for case in cases:
        identity_id, _ = store.upsert_identity(identity_for(case))
        rows.append((identity_id, case))
    store.record_executions(run_pk, rows)
    reconcile_renames(store, run_pk)
    return run_pk


def case(
    name: str, path: str = "tests/test_auth.py", outcome: Outcome = Outcome.PASS
) -> TestCaseResult:
    return TestCaseResult(name=name, file_path=path, outcome=outcome)


def identity_id_of(store: RunStore, name: str, path: str = "tests/test_auth.py") -> int:
    identity_id = store.identity_id_by_fingerprint(identity_for(case(name, path)).fingerprint)
    assert identity_id is not None
    return identity_id


# --- history preservation --------------------------------------------------


def test_renaming_a_test_preserves_its_history(store: RunStore) -> None:
    for index in range(3):
        record_run(store, index, [case("test_login_succeeds", outcome=Outcome.FAIL)])
    record_run(store, 3, [case("test_signin_succeeds", outcome=Outcome.PASS)])

    new_id = identity_id_of(store, "test_signin_succeeds")
    group = store.identity_group(new_id)

    assert group.is_merged
    assert len(store.executions_for_group(new_id)) == 4
    # The rename was inferred, not observed, so the merge is labelled.
    assert group.merged_uncertain is True


def test_history_is_reachable_from_either_end_of_the_alias(store: RunStore) -> None:
    record_run(store, 0, [case("test_login_succeeds")])
    record_run(store, 1, [case("test_signin_succeeds")])

    old_id = identity_id_of(store, "test_login_succeeds")
    new_id = identity_id_of(store, "test_signin_succeeds")
    assert store.identity_group(old_id).identity_ids == store.identity_group(new_id).identity_ids
    assert len(store.executions_for_group(old_id)) == 2


def test_a_chain_of_renames_merges_transitively(store: RunStore) -> None:
    """Three names over three runs is still one test."""
    record_run(store, 0, [case("test_login_succeeds")])
    record_run(store, 1, [case("test_signin_succeeds")])
    record_run(store, 2, [case("test_signin_succeeded")])

    latest = identity_id_of(store, "test_signin_succeeded")
    group = store.identity_group(latest)
    assert len(group.identity_ids) == 3
    assert len(store.executions_for_group(latest)) == 3


def test_a_file_move_preserves_history(store: RunStore) -> None:
    record_run(store, 0, [case("test_login", path="tests/test_login.py")])
    record_run(store, 1, [case("test_login", path="tests/auth/test_login.py")])

    moved = identity_id_of(store, "test_login", "tests/auth/test_login.py")
    assert len(store.executions_for_group(moved)) == 2


def test_group_history_respects_the_window(store: RunStore) -> None:
    for index in range(4):
        record_run(store, index, [case("test_login_succeeds")])
    record_run(store, 4, [case("test_signin_succeeds")])

    new_id = identity_id_of(store, "test_signin_succeeds")
    assert len(store.executions_for_group(new_id, limit=2)) == 2


def test_group_history_is_ordered_newest_first(store: RunStore) -> None:
    record_run(store, 0, [case("test_login_succeeds", outcome=Outcome.FAIL)])
    record_run(store, 1, [case("test_signin_succeeds", outcome=Outcome.PASS)])

    records = store.executions_for_group(identity_id_of(store, "test_signin_succeeds"))
    assert [record.run_id for record in records] == ["run-1", "run-0"]


# --- no false merges -------------------------------------------------------


def test_an_unrelated_new_test_starts_a_fresh_history(store: RunStore) -> None:
    record_run(store, 0, [case("test_login")])
    record_run(store, 1, [case("test_login"), case("test_invoice_totals_are_rounded")])

    fresh = identity_id_of(store, "test_invoice_totals_are_rounded")
    assert store.identity_group(fresh).is_merged is False
    assert len(store.executions_for_group(fresh)) == 1


def test_a_test_that_still_runs_is_not_treated_as_disappeared(store: RunStore) -> None:
    """Only identities absent from this run are candidates."""
    record_run(store, 0, [case("test_login_succeeds")])
    record_run(store, 1, [case("test_login_succeeds"), case("test_login_succeeded")])

    new_id = identity_id_of(store, "test_login_succeeded")
    assert store.identity_group(new_id).is_merged is False


def test_a_skipped_test_has_not_disappeared(store: RunStore) -> None:
    """A skip is still an execution row, so the test is present, not renamed."""
    record_run(store, 0, [case("test_login_succeeds")])
    record_run(
        store,
        1,
        [case("test_login_succeeds", outcome=Outcome.SKIP), case("test_login_succeeded")],
    )
    assert store.identity_group(identity_id_of(store, "test_login_succeeded")).is_merged is False


def test_ambiguous_renames_leave_both_histories_alone(store: RunStore) -> None:
    record_run(store, 0, [case("test_login_a")])
    record_run(store, 1, [case("test_login_b"), case("test_login_c")])

    for name in ("test_login_b", "test_login_c"):
        assert store.identity_group(identity_id_of(store, name)).is_merged is False


def test_a_certain_merge_is_not_labelled_uncertain(store: RunStore) -> None:
    """A one-character typo fix is close enough to merge without a caveat."""
    record_run(store, 0, [case("test_login_succeedss")])
    record_run(store, 1, [case("test_login_succeeds")])

    group = store.identity_group(identity_id_of(store, "test_login_succeeds"))
    assert group.is_merged
    assert group.merged_uncertain is False


def test_reconciling_the_first_run_records_nothing(store: RunStore) -> None:
    result_pk = record_run(store, 0, [case("test_login")])
    assert reconcile_renames(store, result_pk).recorded == 0


def test_reconciling_an_empty_run_records_nothing(store: RunStore) -> None:
    record_run(store, 0, [case("test_login")])
    empty_pk = record_run(store, 1, [])
    assert reconcile_renames(store, empty_pk).recorded == 0


def test_recording_the_same_alias_twice_is_idempotent(store: RunStore) -> None:
    record_run(store, 0, [case("test_login_succeeds")])
    run_pk = record_run(store, 1, [case("test_signin_succeeds")])

    reconcile_renames(store, run_pk)
    edges = store.connection.execute("SELECT COUNT(*) AS n FROM identity_aliases").fetchone()["n"]
    assert edges == 1


def test_an_uncertain_edge_can_be_upgraded_but_not_downgraded(store: RunStore) -> None:
    """Better evidence should firm up a merge; weaker evidence must not undo it."""
    record_run(store, 0, [case("test_login_succeeds")])
    record_run(store, 1, [case("test_signin_succeeds")])

    old_id = identity_id_of(store, "test_login_succeeds")
    new_id = identity_id_of(store, "test_signin_succeeds")
    assert store.identity_group(new_id).merged_uncertain is True

    store.record_alias(old_id, new_id, similarity=0.99, certain=True)
    assert store.identity_group(new_id).merged_uncertain is False

    store.record_alias(old_id, new_id, similarity=0.5, certain=False)
    assert store.identity_group(new_id).merged_uncertain is False


# --- property-based --------------------------------------------------------

_NAME_CHARS = string.ascii_lowercase + "_"
stable_names = st.text(alphabet=_NAME_CHARS, min_size=12, max_size=30)
positions = st.integers(min_value=0, max_value=29)
replacements = st.sampled_from(string.ascii_lowercase)
run_counts = st.integers(min_value=1, max_value=5)


@given(name=stable_names, position=positions, replacement=replacements, runs=run_counts)
@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_a_single_character_rename_preserves_history(
    name: str, position: int, replacement: str, runs: int
) -> None:
    """One substitution in a name of >=12 characters is always inside the threshold.

    A fresh store per example keeps the property about renames rather than about
    accumulated state across examples.
    """
    index = position % len(name)
    renamed = name[:index] + replacement + name[index + 1 :]

    with RunStore.open(IN_MEMORY) as store:
        for run in range(runs):
            record_run(store, run, [case(name, outcome=Outcome.FAIL)])
        record_run(store, runs, [case(renamed)])

        new_id = identity_id_of(store, renamed)
        # Either way, all runs plus the post-rename run must be visible from the
        # new name. When the substitution happens to be a no-op the identity is
        # simply unchanged, which is history preservation by construction.
        if renamed != name:
            assert store.identity_group(new_id).is_merged, (
                f"{name!r} -> {renamed!r} lost its history"
            )
        assert len(store.executions_for_group(new_id)) == runs + 1


@given(first=stable_names, second=stable_names)
@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_dissimilar_names_are_never_merged(first: str, second: str) -> None:
    """The complementary guarantee: no silent merge of two unrelated tests."""
    from flaketriage.identity.similarity import normalized_distance

    # Only assert on pairs the rule is meant to reject: half the name distance
    # alone (the path is identical) must exceed the configured ceiling.
    if 0.5 * normalized_distance(first, second) <= 0.25:
        return

    with RunStore.open(IN_MEMORY) as store:
        record_run(store, 0, [case(first)])
        record_run(store, 1, [case(second)])
        assert store.identity_group(identity_id_of(store, second)).is_merged is False

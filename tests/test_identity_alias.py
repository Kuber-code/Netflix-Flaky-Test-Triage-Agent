"""Rename detection rules, tested without a database."""

from __future__ import annotations

from flaketriage.config import IdentityConfig
from flaketriage.identity.alias import combined_distance, detect_renames
from flaketriage.identity.fingerprint import fingerprint
from flaketriage.models import TestIdentity

CONFIG = IdentityConfig()


def ident(suite: str, name: str, params: str = "") -> TestIdentity:
    return TestIdentity(
        fingerprint=fingerprint(suite, name, params),
        suite_path=suite,
        test_name=name,
        parameters=params,
        file_path=suite.split("::")[0] or None,
    )


def test_rename_within_a_file_is_detected() -> None:
    old = ident("tests/test_auth.py", "test_login_succeeds")
    new = ident("tests/test_auth.py", "test_signin_succeeds")

    (candidate,) = detect_renames([old], [new], CONFIG)
    assert candidate.old_fingerprint == old.fingerprint
    assert candidate.new_fingerprint == new.fingerprint


def test_file_move_with_an_unchanged_name_is_detected() -> None:
    """The motivating case: the test did not change, its path did."""
    old = ident("tests/test_login.py", "test_login")
    new = ident("tests/auth/test_login.py", "test_login")

    (candidate,) = detect_renames([old], [new], CONFIG)
    assert candidate.similarity > 0.85


def test_a_rename_and_a_move_at_once_is_refused() -> None:
    """Two coincident changes is genuinely ambiguous; refusing is the honest call."""
    old = ident("tests/test_login.py", "test_login_succeeds")
    new = ident("tests/integration/auth/test_signin_flow.py", "test_signin_works_now")

    assert detect_renames([old], [new], CONFIG) == []


def test_unrelated_tests_are_not_merged() -> None:
    old = ident("tests/test_auth.py", "test_login")
    new = ident("tests/test_billing.py", "test_invoice_totals")
    assert detect_renames([old], [new], CONFIG) == []


def test_parameter_instances_are_never_renames_of_each_other() -> None:
    old = ident("tests/test_auth.py", "test_login", "user=alice")
    new = ident("tests/test_auth.py", "test_login", "user=bob")
    assert detect_renames([old], [new], CONFIG) == []


def test_parameters_are_carried_through_a_rename() -> None:
    old = ident("tests/test_auth.py", "test_login", "user=alice")
    new = ident("tests/test_auth.py", "test_signin", "user=alice")
    assert len(detect_renames([old], [new], CONFIG)) == 1


def test_close_matches_are_certain_and_distant_ones_are_not() -> None:
    """Only near-identical evidence earns a silent merge."""
    typo_fix = detect_renames(
        [ident("tests/test_auth.py", "test_loginn_succeeds")],
        [ident("tests/test_auth.py", "test_login_succeeds")],
        CONFIG,
    )
    assert typo_fix[0].certain is True

    rewrite = detect_renames(
        [ident("tests/test_auth.py", "test_login")],
        [ident("tests/test_auth.py", "test_signin")],
        CONFIG,
    )
    assert rewrite[0].certain is False


def test_ambiguous_competition_merges_nothing() -> None:
    """Two equally good explanations mean there is no evidence to choose."""
    old = ident("tests/test_auth.py", "test_login_a")
    rival_one = ident("tests/test_auth.py", "test_login_b")
    rival_two = ident("tests/test_auth.py", "test_login_c")

    assert detect_renames([old], [rival_one, rival_two], CONFIG) == []


def test_a_clear_winner_beats_a_worse_rival() -> None:
    old = ident("tests/test_auth.py", "test_login_succeeds")
    near = ident("tests/test_auth.py", "test_login_succeeded")
    far = ident("tests/test_auth.py", "test_login_x")

    (candidate,) = detect_renames([old], [near, far], CONFIG)
    assert candidate.new_fingerprint == near.fingerprint


def test_each_identity_is_used_at_most_once() -> None:
    old_one = ident("tests/test_auth.py", "test_alpha_one")
    old_two = ident("tests/test_auth.py", "test_beta_two")
    new_one = ident("tests/test_auth.py", "test_alpha_1")
    new_two = ident("tests/test_auth.py", "test_beta_2")

    candidates = detect_renames([old_one, old_two], [new_one, new_two], CONFIG)
    assert len({c.old_fingerprint for c in candidates}) == len(candidates)
    assert len({c.new_fingerprint for c in candidates}) == len(candidates)


def test_empty_inputs_are_handled() -> None:
    one = ident("tests/test_auth.py", "test_login")
    assert detect_renames([], [one], CONFIG) == []
    assert detect_renames([one], [], CONFIG) == []
    assert detect_renames([], [], CONFIG) == []


def test_candidates_are_ordered_by_confidence() -> None:
    olds = [
        ident("tests/test_a.py", "test_one_alpha"),
        ident("tests/test_b.py", "test_two_bravo"),
    ]
    news = [
        ident("tests/test_a.py", "test_one_alphaa"),
        ident("tests/test_b.py", "test_two_brav"),
    ]
    candidates = detect_renames(olds, news, CONFIG)
    distances = [candidate.distance for candidate in candidates]
    assert distances == sorted(distances)


def test_combined_distance_weights_name_and_path_equally() -> None:
    """A pure rename and a pure move of the same magnitude score the same.

    Equal-length strings on both axes, so the two normalized distances are
    directly comparable and only the weighting is under test.
    """
    base = ident("aaaa", "aaaa")
    renamed = ident("aaaa", "bbbb")
    moved = ident("bbbb", "aaaa")
    assert combined_distance(base, renamed) == combined_distance(base, moved) == 0.5


def test_thresholds_come_from_config_not_from_code() -> None:
    old = ident("tests/test_auth.py", "test_login")
    new = ident("tests/test_auth.py", "test_signin")

    assert detect_renames([old], [new], IdentityConfig(alias_max_distance=0.0)) == []
    permissive = IdentityConfig(alias_max_distance=0.9, alias_certain_distance=0.9)
    assert detect_renames([old], [new], permissive)[0].certain is True

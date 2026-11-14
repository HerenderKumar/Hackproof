"""Verdict aggregation (section 6.3): blocked > fail > unverified > pass."""

import pytest

from core.verdicts import PRECEDENCE, Verdict, claim_verdicts, worst


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        (["pass", "blocked", "fail", "unverified"], "blocked"),
        (["pass", "fail", "unverified"], "fail"),
        (["pass", "unverified"], "unverified"),
        (["unverified", "fail"], "fail"),
        (["pass", "pass"], "pass"),
        ([], "unverified"),
    ],
)
def test_worst_follows_section_6_3(verdicts: list[Verdict], expected: Verdict) -> None:
    assert worst(verdicts) == expected


def test_the_precedence_is_the_briefs() -> None:
    assert PRECEDENCE == ("blocked", "fail", "unverified", "pass")


def test_a_claim_without_results_is_unverified() -> None:
    assert claim_verdicts(["c1", "c2"], [("c1", "pass")]) == {"c1": "pass", "c2": "unverified"}


def test_one_failing_case_fails_the_claim() -> None:
    results: list[tuple[str, Verdict]] = [("c1", "pass"), ("c1", "fail"), ("c1", "pass")]

    assert claim_verdicts(["c1"], results) == {"c1": "fail"}


def test_results_for_claims_not_listed_are_ignored() -> None:
    assert claim_verdicts(["c1"], [("c1", "pass"), ("c9", "blocked")]) == {"c1": "pass"}

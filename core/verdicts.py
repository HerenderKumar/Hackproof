"""Verdicts (section 6.3). A verdict summarises recorded evidence; the judge makes the call.

Per claim, its tests aggregate with the precedence `blocked > fail > unverified > pass`, and a
claim with nothing recorded is `unverified`. Claim verdicts are computed when read, never
stored, so they cannot drift from the rows they summarise.
"""

from collections.abc import Iterable, Sequence
from typing import Literal

Verdict = Literal["pass", "fail", "blocked", "unverified"]

PRECEDENCE: tuple[Verdict, ...] = ("blocked", "fail", "unverified", "pass")


def worst(verdicts: Iterable[Verdict]) -> Verdict:
    """The verdict that wins under section 6.3's precedence; `unverified` for none."""
    found = set(verdicts)
    return next((verdict for verdict in PRECEDENCE if verdict in found), "unverified")


def claim_verdicts(
    claim_ids: Sequence[str], results: Iterable[tuple[str, Verdict]]
) -> dict[str, Verdict]:
    """Every listed claim's verdict from `(claim_id, verdict)` pairs."""
    by_claim: dict[str, list[Verdict]] = {claim: [] for claim in claim_ids}
    for claim, verdict in results:
        if claim in by_claim:
            by_claim[claim].append(verdict)
    return {claim: worst(verdicts) for claim, verdicts in by_claim.items()}

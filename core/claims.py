"""A team's claims version: what it said it would build (D-047, section 5.2).

A version is the whole start text: the problem statement, the explanation and one to three
claims. Its hash covers all three, so a receipt pins everything the tests were drafted from.
"""

from core.hashing import hash_json

CLAIM_FIELDS = ("id", "text", "target", "example_input", "expected_behaviour")


def claims_hash(problem_statement: str, explanation: str, items: list[dict[str, str]]) -> str:
    """SHA-256 of the canonical JSON of the start text."""
    return hash_json(
        {"problem_statement": problem_statement, "explanation": explanation, "items": items}
    )

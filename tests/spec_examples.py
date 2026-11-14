"""Complete API specs for tests, as the plan's fixture table writes them (M5, Task 8).

Each meets the case rules: one practice case, three hidden cases, two of them edge cases.
"""

# Specs read best one step or case per line.
# ruff: noqa: E501

from typing import Any


def _status(code: int) -> dict[str, Any]:
    return {"op": "status_equals", "value": code}


F01: dict[str, Any] = {
    "kind": "api",
    "claim_id": "c1",
    "claim_type": "calculation",
    "steps": [
        {
            "id": "split",
            "request": {
                "method": "POST",
                "path": "/split",
                "json": {"total": "{{case.input.total}}", "people": "{{case.input.people}}"},
            },
            "assertions": [
                _status(200),
                {"op": "length_equals", "path": "$.shares", "value": "{{case.input.people}}"},
                {"op": "sum_equals", "path": "$.shares[*]", "value": "{{case.input.total}}", "tolerance": 0},
            ],
        }
    ],
    "practice_cases": [{"id": "p1", "input": {"total": 100, "people": 4}}],
    "final_cases": [
        {"id": "f1", "input": {"total": 100.00, "people": 3}},
        {"id": "f2", "input": {"total": 0.01, "people": 2}, "boundary": True},
        {"id": "f3", "input": {"total": 1000, "people": 7}, "boundary": True},
    ],
}  # fmt: skip

F02: dict[str, Any] = {
    "kind": "api",
    "claim_id": "c1",
    "claim_type": "create_update",
    "steps": [
        {
            "id": "create",
            "request": {
                "method": "POST",
                "path": "/items",
                "json": {"name": "{{case.input.name}}", "quantity": "{{case.input.quantity}}"},
            },
            "save": {"id": "$.id"},
            "assertions": [_status(201)],
        },
        {
            "id": "read",
            "request": {"method": "GET", "path": "/items/{{saved.id}}"},
            "assertions": [
                _status(200),
                {"op": "equals", "path": "$.name", "value": "{{case.input.name}}"},
                {"op": "equals", "path": "$.quantity", "value": "{{case.input.quantity}}"},
            ],
        },
    ],
    "practice_cases": [{"id": "p1", "input": {"name": "rice", "quantity": 2}}],
    "final_cases": [
        {"id": "f1", "input": {"name": "lentils", "quantity": 5}},
        {"id": "f2", "input": {"name": "a", "quantity": 0}, "boundary": True},
        {"id": "f3", "input": {"name": "Bread & butter (salted)", "quantity": 1000}, "boundary": True},
    ],
}  # fmt: skip

F03: dict[str, Any] = {
    "kind": "api",
    "claim_id": "c1",
    "claim_type": "classification",
    "steps": [
        {
            "id": "classify",
            "request": {"method": "POST", "path": "/classify", "json": {"text": "{{case.input.text}}"}},
            "assertions": [_status(200), {"op": "equals", "path": "$.label", "value": "{{case.expected.label}}"}],
        }
    ],
    "practice_cases": [{"id": "p1", "input": {"text": "I love this great place"}, "expected": {"label": "positive"}}],
    "final_cases": [
        {"id": "f1", "input": {"text": "The soup was bland and the bread was poor"}, "expected": {"label": "negative"}},
        {"id": "f2", "input": {"text": "good food but bad service"}, "expected": {"label": "positive"}, "boundary": True},
        {"id": "f3", "input": {"text": "TERRIBLE, AWFUL and BLAND!"}, "expected": {"label": "negative"}, "boundary": True},
    ],
}  # fmt: skip

_DEMO = {"username": "demo", "password": "hackathon"}

F04: dict[str, Any] = {
    "kind": "api",
    "claim_id": "c1",
    "claim_type": "authentication",
    "steps": [
        {
            "id": "login",
            "request": {
                "method": "POST",
                "path": "/login",
                "json": {"username": "{{case.input.username}}", "password": "{{case.input.password}}"},
            },
            "save": {"token": "$.token"},
            "assertions": [_status(200)],
        },
        {
            "id": "me",
            "request": {"method": "GET", "path": "/me", "headers": {"Authorization": "Bearer {{saved.token}}"}},
            "assertions": [_status(200), {"op": "equals", "path": "$.username", "value": "{{case.input.username}}"}],
        },
        {
            "id": "refuse",
            "request": {"method": "GET", "path": "/me", "headers": {"Authorization": "{{case.input.bad_auth}}"}},
            "assertions": [_status(401)],
        },
    ],
    "practice_cases": [{"id": "p1", "input": _DEMO | {"bad_auth": "Bearer wrong-token"}}],
    "final_cases": [
        {"id": "f1", "input": _DEMO | {"bad_auth": "Bearer 12345"}},
        {"id": "f2", "input": _DEMO | {"bad_auth": ""}, "boundary": True},
        {"id": "f3", "input": _DEMO | {"bad_auth": "Basic ZGVtbzpoYWNrYXRob24="}, "boundary": True},
    ],
}  # fmt: skip

F07: dict[str, Any] = {
    "kind": "api",
    "claim_id": "c1",
    "claim_type": "calculation",
    "steps": [
        {
            "id": "count",
            "request": {"method": "POST", "path": "/wordcount", "json": {"text": "{{case.input.text}}"}},
            "assertions": [_status(200), {"op": "equals", "path": "$.words", "value": "{{case.expected.words}}"}],
        }
    ],
    "practice_cases": [{"id": "p1", "input": {"text": "the quick brown fox"}, "expected": {"words": 4}}],
    "final_cases": [
        {"id": "f1", "input": {"text": "one two three"}, "expected": {"words": 3}},
        {"id": "f2", "input": {"text": ""}, "expected": {"words": 0}, "boundary": True},
        {"id": "f3", "input": {"text": "  spaced   out\ttext\n"}, "expected": {"words": 3}, "boundary": True},
    ],
}  # fmt: skip

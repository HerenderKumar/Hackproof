"""A run's model use through the door, and whose each refusal was (Prune design, B3)."""

import json
from typing import Any

import httpx
import pytest

from core.errors import InfraError
from core.prune import PruneClient
from worker.model_use import summarise, with_receipt

OK = {"route": "/v1/chat/completions", "model": "gpt-4.1-nano", "status": 200,
      "prompt_tokens": 10, "completion_tokens": 4, "prune_cost_with_prune_usd": 0.0002,
      "prune_cache_hit": False, "prune_optimizations_applied": []}  # fmt: skip


def line(**fields: Any) -> dict[str, Any]:
    return {"route": "/v1/chat/completions", "model": "gpt-4.1-nano", **fields}


def test_successful_calls_are_counted_with_their_tokens_cost_and_models() -> None:
    use = summarise([OK, OK, {**OK, "model": "claude-haiku-4-5", "route": "/v1/messages"}])
    assert (use["calls"], use["calls_ok"], use["refused"]) == (3, 3, [])
    assert use["models"] == {"claude-haiku-4-5": 1, "gpt-4.1-nano": 2}
    assert use["tokens"] == {"completion_tokens": 12, "prompt_tokens": 30}
    assert use["cost_usd"] == 0.0006 and use["optimised"] is False


def test_prune_s_no_op_steps_and_its_false_header_are_not_optimisation() -> None:
    # As the real Prune logs a call it changed nothing in (seen live, 2026-11-15).
    seen = {**OK, "route_applied": "false", "prune_optimizations_applied": [
        "template_hoist_skip:no_recurring_prefix", "token_budget_analyze",
        "output_cap:not_tightened", "prefix_broker_skip:no_stable_prefix"]}  # fmt: skip
    assert summarise([seen])["optimised"] is False
    assert summarise([{**seen, "route_applied": "true"}])["optimised"] is True
    assert (
        summarise([{**OK, "prune_optimizations_applied": ["output_cap:tightened"]}])["optimised"]
        is True
    )
    assert summarise([{**OK, "prune_cache_hit": True}])["optimised"] is True


def test_no_calls_is_not_a_failure() -> None:
    use = summarise([])
    assert (use["calls"], use["refused"], use["outside"]["connections"]) == (0, [], 0)


@pytest.mark.parametrize(
    ("failed", "reason"),
    [
        (line(status=429, shield_code="run_budget"), "this run's model budget was used up"),
        (line(status=429, shield_code="agent_step_budget"), "this run's model steps were used up"),
        (line(status=403, shield_code="tool_policy"), "a tool the event does not allow"),
        (line(status=403, shield_code="prompt_injection"), "looked like an injection"),
        (line(status=400, shield_code="some_new_content_block"), "Shield blocked a call: some_new"),
        (line(status=400, refused="model_not_allowed"), "a model the event does not allow"),
        (line(status=413, refused="body_too_large"), "larger than the door takes"),
        (line(status=429, refused="call_cap"), "this run's model calls were used up"),
        (line(status=401, refused="token"), "without the key HackProof gave the app"),
        (line(status=400, error_code="invalid_request"), "the model service refused a call (400)"),
        (
            {"egress": "api.other.test", "port": 443, "status": 403, "refused": "not on the list"},
            "an outside call",
        ),
    ],
)
def test_the_team_s_refusals_stand_and_are_said_in_words(
    failed: dict[str, Any], reason: str
) -> None:
    use = summarise([OK, failed])
    (refused,) = use["refused"]
    assert reason in refused["reason"] and refused["count"] == 1


@pytest.mark.parametrize(
    "failed",
    [
        line(status=502, unreachable="ConnectError"),
        line(status=401),  # our Prune key
        line(status=402, shield_code="plan_limit"),  # our plan
        line(status=429, shield_code="spend_cap"),  # our account's cap
        line(status=429, shield_code="rate_limit"),
        line(status=503),
        line(status=404),  # a model on our list the provider does not serve
        line(status=400, error_code="missing_provider_key"),
        line(status=403, shield_code="model_blocked"),  # our key's model list
    ],
)
def test_a_failure_of_ours_is_an_infrastructure_error(failed: dict[str, Any]) -> None:
    with pytest.raises(InfraError, match="model door"):
        summarise([OK, failed])


def test_outside_connections_are_counted_by_host() -> None:
    lines = [
        {
            "egress": "api.weather.test",
            "port": 443,
            "status": 200,
            "bytes_up": 10,
            "bytes_down": 900,
        },
        {
            "egress": "api.weather.test",
            "port": 443,
            "status": 200,
            "bytes_up": 10,
            "bytes_down": 900,
        },
        {
            "egress": "evil.test",
            "port": 443,
            "status": 403,
            "refused": "evil.test is not on the list",
        },
    ]
    use = summarise(lines)
    assert use["calls"] == 0
    assert use["outside"] == {"connections": 2, "refused": 1, "hosts": {"api.weather.test": 2}}


def prune_answering(receipt: dict[str, Any] | None, valid: bool) -> PruneClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer prune_key"
        if request.url.path == "/v1/runs/attempt-1/receipt":
            return httpx.Response(200, json=receipt) if receipt else httpx.Response(404, json={})
        if request.url.path == "/v1/receipts/verify":
            assert json.loads(request.content)["receipt"] == receipt
            return httpx.Response(
                200, json={"valid": valid, "reason": "valid" if valid else "bad_signature"}
            )
        return httpx.Response(404)

    return PruneClient("https://prune.test", "prune_key", transport=httpx.MockTransport(answer))


RECEIPT = {
    "run_id": "attempt-1",
    "steps": [{"n": 1}],
    "receipt_signature": "sig",
    "receipt_signing_key_id": "k",
}


async def test_a_verified_receipt_is_stored_with_how_it_was_checked() -> None:
    async with prune_answering(RECEIPT, True) as prune:
        use = await with_receipt(summarise([OK]), prune, "attempt-1")
    assert use["receipt"] == RECEIPT and use["receipt_verified"] is True
    assert use["receipt_check"] == "prune_endpoint"


async def test_a_forged_receipt_is_stored_as_not_verified_and_the_run_stands() -> None:
    forged = {**RECEIPT, "steps": [{"n": 1}, {"n": 2}]}
    async with prune_answering(forged, False) as prune:
        use = await with_receipt(summarise([OK]), prune, "attempt-1")
    assert use["receipt"] == forged and use["receipt_verified"] is False


async def test_a_receipt_that_cannot_be_read_is_not_verified() -> None:
    async with prune_answering(None, True) as prune:
        use = await with_receipt(summarise([OK]), prune, "attempt-1")
    assert (use["receipt"], use["receipt_verified"]) == (None, False)


async def test_a_run_with_no_calls_asks_prune_for_nothing() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("asked Prune")

    async with PruneClient(
        "https://prune.test", "k", transport=httpx.MockTransport(refuse)
    ) as prune:
        use = await with_receipt(summarise([]), prune, "attempt-1")
    assert (use["receipt"], use["receipt_verified"]) == (None, None)

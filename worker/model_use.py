"""What a run's app used through the model door, and whose a refusal was (Prune design, B3).

The door logs one line per model call and per outside connection. `summarise` turns them into
`run_results.model_use`, and decides, by the design's failure table, whether anything that went
wrong was ours or the team's:

- ours (Prune unreachable, our key or plan refused, an outage, our account's limits, a model on
  our list the provider does not serve, no provider key in Prune's vault): an `InfraError`, so
  the run is retried and no verdict is stored from it;
- the team's (the run's budget or step cap, a Shield block on content or tools, a model not on
  the list, an oversized body, a host not on the event's list): the tests' results stand and
  the reason is stored, in words, for the team and the judge.

The receipt is then read from Prune and its signature checked. A receipt that cannot be read or
does not verify never fails the run: it is stored as not verified, with a warning in the log.
Model use never changes a verdict, and 0 calls is not a failure.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Any

from core.errors import InfraError
from core.logging import get_logger
from core.prune import PruneClient

log = get_logger()

# Prune's Shield codes that are the team's app's doing (docs/errors, read 2026-11-15).
TEAM_CODES = {
    "run_budget": "this run's model budget was used up",
    "agent_step_budget": "this run's model steps were used up",
    "call_budget": "this run's model calls were used up",
    "token_budget": "this run's tokens were used up",
    "customer_budget": "this team's model budget was used up",
    "loop_detected": "the app called the model in a loop",
    "repeat_loop": "the app sent the same call again and again",
    "agent_loop": "the app called the model in a loop",
    "tool_policy": "the app asked the model to call a tool the event does not allow",
    "prompt_injection": "a prompt looked like an injection and was blocked",
    "prompt_injection_signal": "a prompt looked like an injection and was blocked",
    "canary_leak": "a prompt carried a protected token and was blocked",
    "card_readback_blocked": "a prompt carried card details and was blocked",
}
# Prune's codes for our account, our key's settings, or Prune itself (docs/errors): ours.
OUR_CODES = frozenset(
    {"spend_cap", "plan_limit", "free_trial_ended", "rate_limit", "ip_blocked", "anomaly_spike"}
    | {"provider_blocked", "model_blocked", "shield_unavailable", "shield_preflight_timeout"}
    | {"shield_preflight_unavailable", "retry_later", "run_budget_missing_run_id"}
)
# Refusals of the door itself, all the team's.
DOOR_REASONS = {
    "token": "a call came without the key HackProof gave the app",
    "model_not_allowed": "a call asked for a model the event does not allow",
    "call_cap": "this run's model calls were used up",
    "body_too_large": "a call was larger than the door takes",
    "bad_body": "a call was not a JSON request with a model",
    "route": "a call went to a route the door does not answer",
}


def _whose(line: dict[str, Any]) -> tuple[str, str] | None:
    """('ours' | 'team', reason) for a line that did not succeed; None for a success."""
    status = int(line.get("status", 0))
    if "egress" in line:
        if status == 200:
            return None
        if "refused" in line:
            return "team", f"an outside call was refused: {line['refused']}"
        return "team", f"the outside host {line['egress']} could not be reached"
    if "refused" in line:
        return "team", DOOR_REASONS.get(str(line["refused"]), str(line["refused"]))
    if "unreachable" in line:
        return "ours", "Prune could not be reached"
    if status == 200:
        return None
    code = line.get("shield_code")
    if code in OUR_CODES:
        return "ours", f"Prune refused the call for our account: {code}"
    if code:
        # Any other block is on what the app sent (content, tools, its run's limits).
        return "team", TEAM_CODES.get(str(code), f"Prune's Shield blocked a call: {code}")
    if line.get("error_code") == "missing_provider_key":
        return "ours", "Prune has no provider key for this model in its vault"
    if status in (401, 402, 403, 404, 429) or status >= 500:
        return "ours", f"Prune or the provider answered {status}"
    return "team", f"the model service refused a call ({status})"


def summarise(lines: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The run's model use from the door's log. Raises `InfraError` if a failure was ours."""
    calls = [line for line in lines if "egress" not in line]
    outside = [line for line in lines if "egress" in line]
    ours = [reason for line in lines if (w := _whose(line)) and w[0] == "ours" for reason in [w[1]]]
    if ours:
        raise InfraError(f"model door: {ours[0]}")
    refusals = Counter(w[1] for line in lines if (w := _whose(line)) and w[0] == "team")
    forwarded = [c for c in calls if "refused" not in c]
    ok = [c for c in forwarded if c.get("status") == 200]
    tokens = Counter[str]()
    for c in ok:
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"):
            if isinstance(c.get(key), int):
                tokens[key] += c[key]
    cost = sum(float(c.get("prune_cost_with_prune_usd") or 0) for c in ok)
    optimised = any(
        c.get("prune_cache_hit") is True
        or c.get("prune_optimizations_applied")
        or c.get("route_applied")
        for c in ok
    )
    return {
        "door": "prune",
        "calls": len(forwarded),
        "calls_ok": len(ok),
        "refused": [{"reason": reason, "count": n} for reason, n in sorted(refusals.items())],
        "models": dict(sorted(Counter(str(c.get("model")) for c in ok).items())),
        "tokens": dict(sorted(tokens.items())),
        "cost_usd": round(cost, 6),
        "optimised": optimised,  # Prune's cache, routing and trimming are asked off
        "outside": {
            "connections": sum(1 for o in outside if o.get("status") == 200),
            "refused": sum(1 for o in outside if o.get("status") != 200),
            "hosts": dict(
                sorted(
                    Counter(str(o.get("egress")) for o in outside if o.get("status") == 200).items()
                )
            ),
        },
    }


async def with_receipt(use: dict[str, Any], prune: PruneClient, run_id: str) -> dict[str, Any]:
    """`use` with the run's receipt and whether Prune's check of its signature held."""
    if use["calls"] == 0:
        return {**use, "receipt": None, "receipt_verified": None}
    try:
        receipt = await prune.run_receipt(run_id)
    except Exception as error:
        log.warning("door_receipt_unread", run_id=run_id, error=type(error).__name__)
        return {**use, "receipt": None, "receipt_verified": False}
    try:
        verified = await prune.verify(receipt)
    except Exception as error:
        log.warning("door_receipt_unchecked", run_id=run_id, error=type(error).__name__)
        verified = False
    if not verified:
        log.warning("door_receipt_not_verified", run_id=run_id)
    return {
        **use,
        "receipt": receipt,
        "receipt_verified": verified,
        "receipt_check": "prune_endpoint",
    }

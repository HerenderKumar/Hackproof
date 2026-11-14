"""Drafting tests from a team's start text (section 15.1 as amended by D-047).

One model call covers all of a team's claims: the problem statement, the explanation and the
claims go in, and for each claim a claim type comes back with a test, or `needs_clarification`
with a reason. The model never sees a repository; no code exists when tests are drafted.

Each drafted test is held to the case rules (`ApiSpec`) and to its claim type's minimum
(`core.spec_rules`). Anything that falls short is asked for again once, with the problems listed.
A test that still falls short becomes `needs_clarification`, never a weak draft. A claim with no
usable answer after the retry is the model's fault, not the team's: an `InfraError`, so the job
is retried.

The team's text is untrusted data (section 12.5): it sits inside tags it cannot close, and the
system prompt tells the model to take no instructions from it.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from core.errors import InfraError
from core.llm import LLMClient, LLMOutputError, Tool
from core.logging import get_logger
from core.spec_rules import problems
from core.specs import ApiSpec, ApiStep, Case

log = get_logger(__name__)

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "draft_specs.md"
_CLAIM_FIELDS = ("text", "target", "example_input", "expected_behaviour")
_COULD_NOT = "HackProof could not draft a test that meets the rules: "
_NO_REASON = "a needs_clarification answer needs a reason that tells the team what to add"


@dataclass(frozen=True)
class StartText:
    """What a team wrote at the start. Each claim has `id` and the claim form's four fields."""

    problem_statement: str
    explanation: str
    claims: list[dict[str, str]]


@dataclass(frozen=True)
class Drafted:
    """The outcome for one claim. `spec` is a complete API spec for a draft, and None otherwise."""

    claim_id: str
    claim_type: str
    status: Literal["draft", "needs_clarification", "ui_pending"]
    reason: str
    spec: dict[str, Any] | None


class DraftedTest(BaseModel):
    claim_id: str
    claim_type: Literal[
        "calculation", "create_update", "classification", "authentication", "ui_workflow"
    ]
    status: Literal["draft", "needs_clarification"]
    reason: str = ""
    steps: list[ApiStep] = []
    practice_cases: list[Case] = []
    final_cases: list[Case] = []


class DraftOutput(BaseModel):
    tests: list[DraftedTest]


SUBMIT_TESTS: Tool[DraftOutput] = Tool(
    "submit_tests",
    "Submit one entry for every claim: its test, or needs_clarification with a reason.",
    DraftOutput,
)


@cache
def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


def render_start(start: StartText) -> str:
    """The user message: the team's text, each part inside a tag it cannot close."""
    parts = [
        f"<problem_statement>\n{_escape(start.problem_statement)}\n</problem_statement>",
        f"<explanation>\n{_escape(start.explanation)}\n</explanation>",
    ]
    for claim in start.claims:
        fields = "\n".join(
            f"<{name}>{_escape(claim.get(name, ''))}</{name}>" for name in _CLAIM_FIELDS
        )
        claim_id = _escape(claim["id"]).replace('"', "&quot;")
        parts.append(f'<claim id="{claim_id}">\n{fields}\n</claim>')
    return "\n\n".join(parts)


async def draft_tests(
    client: LLMClient,
    start: StartText,
    *,
    model: str,
    max_tokens: int,
    job_id: UUID | None = None,
) -> list[Drafted]:
    """One `Drafted` per claim, in the claims' order. Raises `InfraError` when the model gives
    no usable answer for a claim, even after the one retry."""
    wanted = [claim["id"] for claim in start.claims]
    if not wanted:
        return []
    user = render_start(start)

    async def ask(message: str) -> DraftOutput:
        return await client.submit(
            purpose="draft",
            model=model,
            system=system_prompt(),
            user=message,
            tool=SUBMIT_TESTS,
            max_tokens=max_tokens,
            job_id=job_id,
        )

    done: dict[str, Drafted] = {}
    faults: dict[str, _Fault] = {}
    try:
        first = await ask(user)
    except LLMOutputError as exc:
        feedback = (
            f"Your earlier answer could not be used: {_escape(str(exc))}\n"
            "Call submit_tests again with an entry for every claim."
        )
        retry = wanted
    else:
        for claim_id, test in _answers(first, wanted).items():
            outcome = _assess(test) if test is not None else _Fault([_LEFT_OUT], None)
            if isinstance(outcome, Drafted):
                done[claim_id] = outcome
            else:
                faults[claim_id] = outcome
        if not faults:
            return [done[claim_id] for claim_id in wanted]
        feedback = _fault_feedback(faults)
        retry = list(faults)

    log.info("draft_retry", claims=retry, problems=sum(len(f.problems) for f in faults.values()))
    try:
        second = await ask(f"{user}\n\n<feedback>\n{feedback}\n</feedback>")
    except LLMOutputError as exc:
        raise InfraError(f"the model gave no usable tests after one retry: {exc}") from None

    left_out: list[str] = []
    for claim_id, test in _answers(second, retry).items():
        if test is None:
            left_out.append(claim_id)
            continue
        outcome = _assess(test)
        if isinstance(outcome, _Fault):
            reason = _COULD_NOT + "; ".join(outcome.problems)
            outcome = Drafted(claim_id, test.claim_type, "needs_clarification", reason, None)
        done[claim_id] = outcome
    if left_out:
        raise InfraError(f"the model gave no answer for {', '.join(left_out)} after one retry")
    return [done[claim_id] for claim_id in wanted]


# --- judging an answer ---------------------------------------------------------------------------

_LEFT_OUT = "no entry was given for this claim"


@dataclass(frozen=True)
class _Fault:
    """Why a claim's answer cannot stand, with the answer itself when there was one."""

    problems: list[str]
    previous: DraftedTest | None


def _answers(output: DraftOutput, wanted: list[str]) -> dict[str, DraftedTest | None]:
    """The first entry for each wanted claim. Other IDs and repeats are ignored."""
    found: dict[str, DraftedTest | None] = dict.fromkeys(wanted)
    for test in output.tests:
        if test.claim_id in found and found[test.claim_id] is None:
            found[test.claim_id] = test
    return found


def _assess(test: DraftedTest) -> Drafted | _Fault:
    if test.claim_type == "ui_workflow":  # typed now, drafted with the UI tests (M11)
        return Drafted(test.claim_id, test.claim_type, "ui_pending", test.reason.strip(), None)
    if test.status == "needs_clarification":
        reason = test.reason.strip()
        if not reason:
            return _Fault([_NO_REASON], test)
        return Drafted(test.claim_id, test.claim_type, "needs_clarification", reason, None)
    document = {
        "kind": "api",
        **test.model_dump(by_alias=True, exclude={"status", "reason"}),
    }
    try:
        spec = ApiSpec.model_validate(document)
    except ValidationError as exc:
        return _Fault([_message(error) for error in exc.errors()], test)
    found = problems(spec)
    if found:
        return _Fault(found, test)
    return Drafted(test.claim_id, test.claim_type, "draft", "", spec.model_dump(by_alias=True))


def _message(error: Any) -> str:
    text = str(error["msg"]).removeprefix("Value error, ")
    where = ".".join(str(part) for part in error["loc"])
    return f"{where}: {text}" if where else text


# --- feedback ------------------------------------------------------------------------------------


def _fault_feedback(faults: dict[str, _Fault]) -> str:
    blocks = [
        "Your earlier answer had the problems below. Call submit_tests again with entries for "
        "these claims only, and fix every problem."
    ]
    for claim_id, fault in faults.items():
        lines = [f'<claim_feedback id="{_escape(claim_id)}">', "Problems:"]
        lines += [f"- {_escape(problem)}" for problem in fault.problems]
        if fault.previous is not None:
            lines += ["Your earlier answer:", _json_text(fault.previous)]
        lines.append("</claim_feedback>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _json_text(test: DraftedTest) -> str:
    """The answer as JSON that cannot open or close a tag: `<` and `>` as JSON escapes."""
    text = json.dumps(test.model_dump(by_alias=True), ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")

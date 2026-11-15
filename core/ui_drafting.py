"""Drafting browser tests for UI claims (M11 design, part 9).

The first drafting call (`core/drafting.py`) is unchanged: it types each claim, and a claim it
types `ui_workflow` comes back `ui_pending`. When UI drafting is on, those claims go to one second
call with its own prompt (`prompts/draft_ui_specs.md`) and tool (`submit_ui_tests`). Keeping the
calls apart leaves the first call's prompt, tool and recordings exactly as they were (D-050).

Each UI test is held to the case rules (`UiSpec`) and to the `ui_workflow` minimum
(`core.spec_rules.ui_problems`), asked for again once with the problems listed, and otherwise
becomes `needs_clarification`, as API drafting does. A claim with no usable answer after the
retry is the model's fault: an `InfraError`, so the job is retried.

The team's text is untrusted data (section 12.5): the user message is the first call's, with the
claims to draft listed after it.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from core.drafting import Drafted, StartText, draft_tests, escape_tags, render_start
from core.errors import InfraError
from core.llm import LLMClient, LLMOutputError, Tool
from core.logging import get_logger
from core.spec_rules import ui_problems
from core.specs import Case, UiSpec, UiStep

log = get_logger(__name__)

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "draft_ui_specs.md"
_COULD_NOT = "HackProof could not draft a browser test that meets the rules: "
_NO_REASON = "a needs_clarification answer needs a reason that tells the team what to add"
_LEFT_OUT = "no entry was given for this claim"


class DraftedUiTest(BaseModel):
    claim_id: str
    status: Literal["draft", "needs_clarification"]
    reason: str = ""
    steps: list[UiStep] = []
    practice_cases: list[Case] = []
    final_cases: list[Case] = []


class UiDraftOutput(BaseModel):
    tests: list[DraftedUiTest]


SUBMIT_UI_TESTS: Tool[UiDraftOutput] = Tool(
    "submit_ui_tests",
    "Submit one entry for every claim in <ui_claims>: its browser test, or needs_clarification "
    "with a reason.",
    UiDraftOutput,
)


@cache
def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


def render_ui_request(start: StartText, claim_ids: list[str]) -> str:
    listed = ", ".join(escape_tags(claim_id) for claim_id in claim_ids)
    return f"{render_start(start)}\n\n<ui_claims>{listed}</ui_claims>"


async def draft_with_ui(
    client: LLMClient,
    start: StartText,
    *,
    model: str,
    max_tokens: int,
    ui_model: str | None,
    job_id: UUID | None = None,
) -> list[Drafted]:
    """Every claim drafted. UI claims are drafted by a second call when `ui_model` is set, and
    otherwise stay `ui_pending`."""
    drafts = await draft_tests(client, start, model=model, max_tokens=max_tokens, job_id=job_id)
    pending = [draft.claim_id for draft in drafts if draft.status == "ui_pending"]
    if ui_model is None or not pending:
        return drafts
    ui = await draft_ui_tests(
        client, start, pending, model=ui_model, max_tokens=max_tokens, job_id=job_id
    )
    return [ui.get(draft.claim_id, draft) for draft in drafts]


async def draft_ui_tests(
    client: LLMClient,
    start: StartText,
    claim_ids: list[str],
    *,
    model: str,
    max_tokens: int,
    job_id: UUID | None = None,
) -> dict[str, Drafted]:
    """One `Drafted` per listed claim. Raises `InfraError` when the model gives no usable answer
    for a claim, even after the one retry."""
    user = render_ui_request(start, claim_ids)

    async def ask(message: str) -> UiDraftOutput:
        return await client.submit(
            purpose="draft",
            model=model,
            system=system_prompt(),
            user=message,
            tool=SUBMIT_UI_TESTS,
            max_tokens=max_tokens,
            job_id=job_id,
        )

    done: dict[str, Drafted] = {}
    faults: dict[str, tuple[list[str], DraftedUiTest | None]] = {}
    try:
        first = await ask(user)
    except LLMOutputError as exc:
        feedback = (
            f"Your earlier answer could not be used: {escape_tags(str(exc))}\n"
            "Call submit_ui_tests again with an entry for every claim in <ui_claims>."
        )
        retry = list(claim_ids)
    else:
        for claim_id, test in _answers(first, claim_ids).items():
            outcome = _assess(test) if test is not None else ([_LEFT_OUT], None)
            if isinstance(outcome, Drafted):
                done[claim_id] = outcome
            else:
                faults[claim_id] = outcome
        if not faults:
            return done
        feedback = _feedback(faults)
        retry = list(faults)

    log.info("ui_draft_retry", claims=retry)
    try:
        second = await ask(f"{user}\n\n<feedback>\n{feedback}\n</feedback>")
    except LLMOutputError as exc:
        raise InfraError(f"the model gave no usable browser tests after one retry: {exc}") from None
    left_out = []
    for claim_id, test in _answers(second, retry).items():
        if test is None:
            left_out.append(claim_id)
            continue
        outcome = _assess(test)
        if not isinstance(outcome, Drafted):
            reason = _COULD_NOT + "; ".join(outcome[0])
            outcome = Drafted(claim_id, "ui_workflow", "needs_clarification", reason, None)
        done[claim_id] = outcome
    if left_out:
        raise InfraError(f"the model gave no browser test for {', '.join(left_out)} after a retry")
    return done


def _answers(output: UiDraftOutput, wanted: list[str]) -> dict[str, DraftedUiTest | None]:
    """The first entry for each wanted claim. Other IDs and repeats are ignored."""
    found: dict[str, DraftedUiTest | None] = dict.fromkeys(wanted)
    for test in output.tests:
        if test.claim_id in found and found[test.claim_id] is None:
            found[test.claim_id] = test
    return found


def _assess(test: DraftedUiTest) -> Drafted | tuple[list[str], DraftedUiTest | None]:
    if test.status == "needs_clarification":
        reason = test.reason.strip()
        if not reason:
            return [_NO_REASON], test
        return Drafted(test.claim_id, "ui_workflow", "needs_clarification", reason, None)
    document = {
        "kind": "ui",
        "claim_id": test.claim_id,
        "claim_type": "ui_workflow",
        "steps": [step.model_dump(exclude_none=True) for step in test.steps],
        "practice_cases": [case.model_dump() for case in test.practice_cases],
        "final_cases": [case.model_dump() for case in test.final_cases],
    }
    try:
        spec = UiSpec.model_validate(document)
    except ValidationError as exc:
        return [_message(error) for error in exc.errors()], test
    found = ui_problems(spec)
    if found:
        return found, test
    dumped = spec.model_dump(exclude_none=True)
    return Drafted(test.claim_id, "ui_workflow", "draft", "", dumped)


def _message(error: Any) -> str:
    text = str(error["msg"]).removeprefix("Value error, ")
    where = ".".join(str(part) for part in error["loc"])
    return f"{where}: {text}" if where else text


def _feedback(faults: dict[str, tuple[list[str], DraftedUiTest | None]]) -> str:
    blocks = [
        "Your earlier answer had the problems below. Call submit_ui_tests again with entries for "
        "these claims only, and fix every problem."
    ]
    for claim_id, (found, previous) in faults.items():
        lines = [f'<claim_feedback id="{escape_tags(claim_id)}">', "Problems:"]
        lines += [f"- {escape_tags(problem)}" for problem in found]
        if previous is not None:
            text = json.dumps(previous.model_dump(exclude_none=True), ensure_ascii=False)
            lines += ["Your earlier answer:", text.replace("<", "\\u003c").replace(">", "\\u003e")]
        lines.append("</claim_feedback>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

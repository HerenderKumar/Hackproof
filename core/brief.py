"""The judge brief's written part (sections 7 and 15.4): a summary, one line per claim and two
demo questions.

The model writes; code decides what is kept. A demo question survives only when its reference
names a test result or a validated citation that the brief really holds, and there are exactly
two. When the model cannot give two, template questions built from the results take their
place. Verdicts are never the model's: it is shown them and cannot return them (invariant 1).
"""

import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.citations import cut_words
from core.llm import LLMClient, Tool

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "brief.md"
SUMMARY_WORDS = 60
ONE_LINE_WORDS = 25
QUESTION_WORDS = 30
QUESTIONS = 2
BRIEF_MAX_TOKENS = 2000
_VALUE_CHARS = 120
RETRY_NOTE = (
    "Your last answer did not have two demo questions whose ref names an ID from the lists "
    "above. Answer again: exactly two questions, each ref copied from a listed ID."
)


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RefIn(_In):
    kind: Literal["test_result", "citation"]
    id: str = Field(max_length=100)


class QuestionIn(_In):
    question: str = Field(max_length=600)
    ref: RefIn


class ClaimLineIn(_In):
    claim_id: str = Field(max_length=40)
    one_line: str = Field(max_length=600)


class BriefOutput(_In):
    summary: str = Field(max_length=1500)
    claims: list[ClaimLineIn] = Field(default_factory=list, max_length=10)
    demo_questions: list[QuestionIn] = Field(default_factory=list, max_length=6)


SUBMIT_BRIEF = Tool(
    "submit_brief",
    "Hand in the brief: a summary, one plain line per claim and exactly two demo questions.",
    BriefOutput,
)


def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


def known_refs(facts: dict[str, Any]) -> dict[str, set[str]]:
    """The IDs a demo question may point at: this brief's test results and kept citations."""
    results: set[str] = set()
    citations: set[str] = set()
    for claim in facts.get("claims", []):
        results.update(case["result_id"] for case in claim.get("cases", []))
        audit = claim.get("audit") or {}
        citations.update(citation["id"] for citation in audit.get("citations", []))
    return {"test_result": results, "citation": citations}


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _VALUE_CHARS else text[: _VALUE_CHARS - 1] + "…"


def template_questions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Up to two questions made from the results alone, failing tests first (section 15.4)."""
    failing: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    cited: list[dict[str, Any]] = []
    for claim in facts.get("claims", []):
        for case in claim.get("cases", []):
            ref = {"kind": "test_result", "id": case["result_id"]}
            failed = case.get("failed") or []
            if case["verdict"] == "fail" and failed:
                first = failed[0]
                text = (
                    f"Your test with input {_short(case.get('input'))} returned "
                    f"{_short(first.get('actual'))} but expected {_short(first.get('expected'))}. "
                    "Walk us through what happens."
                )
                failing.append({"question": text, "ref": ref})
            elif case["verdict"] in ("unverified", "blocked"):
                why = case.get("error") or "it could not be checked"
                text = f"We could not check “{_short(claim['text'])}”: {_short(why)}. Show us."
                unverified.append({"question": text, "ref": ref})
        for citation in (claim.get("audit") or {}).get("citations", []):
            text = (
                f"Walk us through {citation['path']} lines {citation['start_line']} to "
                f"{citation['end_line']}: what does it do for “{_short(claim['text'])}”?"
            )
            cited.append({"question": text, "ref": {"kind": "citation", "id": citation["id"]}})
    chosen = [*failing, *unverified, *cited][:QUESTIONS]
    if len(chosen) < QUESTIONS:
        for claim in facts.get("claims", [])[: QUESTIONS - len(chosen)]:
            text = f"Show us the app doing “{_short(claim['text'])}”, live."
            chosen.append({"question": text, "ref": None})
    return chosen


def kept(output: BriefOutput, facts: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """What of the model's answer is stored, and whether it gave two usable questions."""
    refs = known_refs(facts)
    claim_ids = [claim["id"] for claim in facts.get("claims", [])]
    lines: dict[str, str] = {}
    for line in output.claims:
        if line.claim_id in claim_ids and line.claim_id not in lines:
            lines[line.claim_id] = cut_words(line.one_line, ONE_LINE_WORDS)
    questions = [
        {
            "question": cut_words(item.question, QUESTION_WORDS),
            "ref": {"kind": item.ref.kind, "id": item.ref.id},
        }
        for item in output.demo_questions
        if item.ref.id in refs[item.ref.kind] and item.question.strip()
    ]
    complete = len(questions) >= QUESTIONS
    content = {
        "summary": cut_words(output.summary, SUMMARY_WORDS),
        "claims": lines,
        "demo_questions": questions[:QUESTIONS] if complete else template_questions(facts),
        "questions_from": "model" if complete else "template",
    }
    return content, complete


def raw_content(facts: dict[str, Any]) -> dict[str, Any]:
    """The written part when there is no usable model answer (the degradation rule)."""
    return {
        "summary": None,
        "claims": {},
        "demo_questions": template_questions(facts),
        "questions_from": "template",
    }


def render_input(facts: dict[str, Any]) -> str:
    """What the model is shown. Everything a team wrote sits inside tags, as data."""
    claims = []
    for claim in facts.get("claims", []):
        audit = claim.get("audit") or {}
        claims.append(
            {
                "claim_id": claim["id"],
                "text": claim["text"],
                "claim_type": claim.get("type"),
                "verdict": claim["verdict"],
                "tests": [
                    {
                        "test_result_id": case["result_id"],
                        "verdict": case["verdict"],
                        "input": case.get("input"),
                        "failed_assertions": [
                            {key: a.get(key) for key in ("op", "path", "expected", "actual")}
                            for a in (case.get("failed") or [])[:3]
                        ],
                        "error": case.get("error"),
                    }
                    for case in claim.get("cases", [])[:6]
                ],
                "audit": {
                    "implementation": audit.get("implementation"),
                    "explanation": audit.get("explanation"),
                    "citations": [
                        {
                            "citation_id": c["id"],
                            "path": c["path"],
                            "lines": [c["start_line"], c["end_line"]],
                            "why": c.get("why"),
                        }
                        for c in audit.get("citations", [])
                    ],
                },
            }
        )
    shown = {
        "team": facts.get("submission", {}).get("team_name"),
        "claims": claims,
        "signals": [signal.get("kind") for signal in facts.get("signals", [])],
        "reuse_declaration": facts.get("reuse_declaration"),
    }
    body = json.dumps(shown, ensure_ascii=False, indent=1, default=str)
    return (
        "<brief_inputs>\n"
        + body.replace("</brief_inputs>", "<\\/brief_inputs>")
        + "\n</brief_inputs>\n\nWrite the brief by calling submit_brief."
    )


async def write_brief(
    client: LLMClient, facts: dict[str, Any], *, model: str, job_id: UUID | None = None
) -> dict[str, Any]:
    """The stored written part. One retry when the questions do not hold; then templates.

    Raises `core.llm.LLMOutputError` when the model's answer cannot be read at all.
    """
    user = render_input(facts)
    content: dict[str, Any] = {}
    for attempt in range(2):
        output = await client.submit(
            purpose="brief",
            model=model,
            system=system_prompt(),
            user=user if attempt == 0 else f"{user}\n\n{RETRY_NOTE}",
            tool=SUBMIT_BRIEF,
            max_tokens=BRIEF_MAX_TOKENS,
            job_id=job_id,
        )
        content, complete = kept(output, facts)
        if complete:
            break
    return content

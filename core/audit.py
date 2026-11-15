"""The claim audit (section 15.2): a budgeted tool loop over the pinned archive.

The model reads the team's code through `list_dir`, `read_file` and `grep` and ends with
`submit_audit`: per claim, whether code implementing it is there, with citations, plus text hints.
It never sees a verdict and its answer sets none (invariant 5); citations are checked in code
afterwards (`core.citations`).

Budgets (D-013): at most `max_steps` requests, `max_tokens` prompt and completion tokens over all
of them, and `timeout_s` seconds. When one runs out, a final request offers only `submit_audit`,
and whatever it returns is `partial`. A bad tool call costs a step and is answered with an error
the model can act on. Gateway failures and missing recordings propagate: the job is retried.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.archive_tools import ArchiveTools
from core.drafting import escape_tags
from core.llm import LLMClient, LLMOutputError, StepReply, Tool, ToolCall, parse_arguments

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "audit.md"
STEP_MAX_TOKENS = 4096
FINAL_REQUEST = (
    "Your budget is used up. Call submit_audit now with what you have found; "
    "mark claims you could not check as unclear."
)
_NUDGE = "Use the tools to read the code, and finish by calling submit_audit."
_CLAIM_FIELDS = ("text", "target", "example_input", "expected_behaviour")

Implementation = Literal["implemented", "partial", "not_found", "unclear"]
HintKind = Literal["other_event", "old_licence_year", "name_mismatch", "other"]


# --- the output schema (section 15.2) ------------------------------------------------------------


class CitationIn(BaseModel):
    path: str
    start_line: int
    end_line: int
    why: str = ""


class ClaimAuditIn(BaseModel):
    claim_id: str
    implementation: Implementation
    explanation: str = ""
    citations: list[CitationIn] = Field(default_factory=list)


class TextHintIn(BaseModel):
    path: str
    line: int
    kind: HintKind
    excerpt: str = ""


class AuditOutput(BaseModel):
    claims: list[ClaimAuditIn]
    text_hints: list[TextHintIn] = Field(default_factory=list)


# --- the tools -----------------------------------------------------------------------------------


class ListDirArgs(BaseModel):
    path: str = ""


class ReadFileArgs(BaseModel):
    path: str
    start_line: int = 1
    end_line: int = 400


class GrepArgs(BaseModel):
    pattern: str
    glob: str = "*"


LIST_DIR = Tool(
    "list_dir", "List a folder of the project. Use an empty path for the top folder.", ListDirArgs
)
READ_FILE = Tool(
    "read_file", "Read numbered lines of a file, at most 400 lines a call.", ReadFileArgs
)
GREP = Tool(
    "grep",
    "Search the project's files for plain text (not a regular expression), ignoring letter "
    "case; at most 50 hits. glob limits the files searched, such as *.py or src/*.",
    GrepArgs,
)
SUBMIT_AUDIT = Tool(
    "submit_audit",
    "Submit the audit: one entry per claim, and any text hints. This ends the audit.",
    AuditOutput,
)
TOOLS: tuple[Tool[Any], ...] = (LIST_DIR, READ_FILE, GREP, SUBMIT_AUDIT)


# --- inputs and results --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditInput:
    event_name: str
    kickoff_at: datetime
    team_name: str
    problem_statement: str
    claims: Sequence[dict[str, str]]


@dataclass(frozen=True)
class AuditBudget:
    max_steps: int
    max_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class AuditRun:
    output: AuditOutput | None
    partial: bool
    steps: int
    tokens: int
    failure: str | None


def system_prompt() -> str:
    return _PROMPT.read_text(encoding="utf-8")


def render_input(given: AuditInput, listing: str) -> str:
    """The first user message: the team's text and the file names, inside tags they cannot close."""
    parts = [
        f"<event>{escape_tags(given.event_name)}, started on "
        f"{given.kickoff_at.date().isoformat()}</event>",
        f"<team>{escape_tags(given.team_name)}</team>",
        f"<problem_statement>\n{escape_tags(given.problem_statement)}\n</problem_statement>",
    ]
    for claim in given.claims:
        fields = "\n".join(
            f"<{name}>{escape_tags(claim.get(name, ''))}</{name}>" for name in _CLAIM_FIELDS
        )
        claim_id = escape_tags(claim["id"]).replace('"', "&quot;")
        parts.append(f'<claim id="{claim_id}">\n{fields}\n</claim>')
    parts.append(f"<top_folder>\n{escape_tags(listing)}\n</top_folder>")
    return "\n\n".join(parts)


# --- the loop ------------------------------------------------------------------------------------


async def run_audit(
    client: LLMClient,
    tools: ArchiveTools,
    given: AuditInput,
    *,
    model: str,
    budget: AuditBudget,
    job_id: UUID | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AuditRun:
    listing = await asyncio.to_thread(tools.list_dir, "")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": render_input(given, listing)},
    ]
    started = clock()
    steps = tokens = 0

    async def ask(offered: Sequence[Tool[Any]]) -> StepReply:
        return await client.step(
            purpose="audit",
            model=model,
            messages=messages,
            tools=offered,
            max_tokens=STEP_MAX_TOKENS,
            job_id=job_id,
        )

    while True:
        steps += 1
        try:
            reply = await ask(TOOLS)
        except LLMOutputError as exc:
            messages.append({"role": "user", "content": f"Your reply could not be used: {exc}. "
                                                        f"{_NUDGE}"})  # fmt: skip
        else:
            tokens += reply.prompt_tokens + reply.completion_tokens
            messages.append(reply.message)
            output, results = await _answer(reply.calls, tools)
            if output is not None:
                return AuditRun(output, False, steps, tokens, None)
            if results:
                messages.extend(results)
            else:
                messages.append({"role": "user", "content": _NUDGE})
        if steps >= budget.max_steps or tokens >= budget.max_tokens:
            break
        if clock() - started >= budget.timeout_s:
            break

    messages.append({"role": "user", "content": FINAL_REQUEST})
    steps += 1
    try:
        reply = await ask([SUBMIT_AUDIT])
    except LLMOutputError as exc:
        return AuditRun(None, True, steps, tokens, f"no audit after the budget: {exc}")
    tokens += reply.prompt_tokens + reply.completion_tokens
    failure = "the model did not call submit_audit after its budget was used up"
    for call in reply.calls:
        if call.name == SUBMIT_AUDIT.name:
            try:
                return AuditRun(parse_arguments(call, SUBMIT_AUDIT), True, steps, tokens, None)
            except LLMOutputError as exc:
                failure = str(exc)
    return AuditRun(None, True, steps, tokens, failure)


async def _answer(
    calls: Sequence[ToolCall], tools: ArchiveTools
) -> tuple[AuditOutput | None, list[dict[str, Any]]]:
    """The submission, if a call made a valid one; otherwise one tool message per call."""
    results: list[dict[str, Any]] = []
    for call in calls:
        if call.name == SUBMIT_AUDIT.name:
            try:
                return parse_arguments(call, SUBMIT_AUDIT), []
            except LLMOutputError as exc:
                content = f"error: {exc}. Fix the arguments and call submit_audit again."
        else:
            content = await _run_tool(call, tools)
        results.append({"role": "tool", "tool_call_id": call.id, "content": content})
    return None, results


async def _run_tool(call: ToolCall, tools: ArchiveTools) -> str:
    runners: dict[str, Callable[[ToolCall], Awaitable[str]]] = {
        LIST_DIR.name: lambda c: _list_dir(c, tools),
        READ_FILE.name: lambda c: _read_file(c, tools),
        GREP.name: lambda c: _grep(c, tools),
    }
    runner = runners.get(call.name)
    if runner is None:
        names = ", ".join(tool.name for tool in TOOLS)
        return f"error: there is no tool called {call.name}; use {names}"
    try:
        return await runner(call)
    except LLMOutputError as exc:
        return f"error: {exc}"


async def _list_dir(call: ToolCall, tools: ArchiveTools) -> str:
    args = parse_arguments(call, LIST_DIR)
    return await asyncio.to_thread(tools.list_dir, args.path)


async def _read_file(call: ToolCall, tools: ArchiveTools) -> str:
    args = parse_arguments(call, READ_FILE)
    return await asyncio.to_thread(tools.read_file, args.path, args.start_line, args.end_line)


async def _grep(call: ToolCall, tools: ArchiveTools) -> str:
    args = parse_arguments(call, GREP)
    return await asyncio.to_thread(tools.grep, args.pattern, args.glob)

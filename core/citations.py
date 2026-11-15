"""Citation validation (section 15.3): code, not the model, decides which citations a judge sees.

A citation is kept only when its normalised path stays inside the archive, names a text file,
`1 <= start_line <= end_line <= line_count`, and the range covers at most 200 lines (invariant 4).
Text hints get the same path check and `1 <= line <= line_count`. What is dropped is counted.
Claims are kept in the team's order, once each; one the model did not answer is listed as
missing, never filled in. Text longer than the brief allows is cut, not rejected.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.archive_tools import ArchiveTools, normalise
from core.audit import AuditOutput, CitationIn, ClaimAuditIn, TextHintIn

MAX_CITATIONS = 5
MAX_HINTS = 10
MAX_RANGE = 200
EXPLANATION_WORDS = 40
WHY_WORDS = 20
EXCERPT_CHARS = 120


@dataclass(frozen=True)
class ValidatedAudit:
    content: dict[str, Any]
    citations_dropped: int


def cut_words(text: str, limit: int) -> str:
    words = text.split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit]) + "…"


def cut_chars(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def validate_audit(
    output: AuditOutput | None,
    tools: ArchiveTools,
    claim_ids: Sequence[str],
    *,
    partial: bool,
    failure: str | None,
    steps: int,
    tokens: int,
    model: str,
) -> ValidatedAudit:
    lines = _LineCounts(tools)
    answers: dict[str, ClaimAuditIn] = {}
    for answer in output.claims if output is not None else []:
        if answer.claim_id in claim_ids and answer.claim_id not in answers:
            answers[answer.claim_id] = answer

    dropped = 0
    claims: list[dict[str, Any]] = []
    for claim_id in claim_ids:
        entry = answers.get(claim_id)
        if entry is None:
            continue
        kept: list[dict[str, Any]] = []
        for citation in entry.citations:
            checked = _citation(citation, lines)
            if checked is None or len(kept) == MAX_CITATIONS:
                dropped += 1
                continue
            kept.append({"id": f"{claim_id}-{len(kept) + 1}", **checked})
        claims.append(
            {
                "claim_id": claim_id,
                "implementation": entry.implementation,
                "explanation": cut_words(entry.explanation, EXPLANATION_WORDS),
                "citations": kept,
            }
        )

    hints: list[dict[str, Any]] = []
    hints_dropped = 0
    for hint in output.text_hints if output is not None else []:
        checked_hint = _hint(hint, lines)
        if checked_hint is None or len(hints) == MAX_HINTS:
            hints_dropped += 1
            continue
        hints.append(checked_hint)

    content = {
        "claims": claims,
        "text_hints": hints,
        "missing_claims": [claim_id for claim_id in claim_ids if claim_id not in answers],
        "hints_dropped": hints_dropped,
        "partial": partial,
        "failure": failure,
        "steps": steps,
        "tokens": tokens,
        "model": model,
    }
    return ValidatedAudit(content, dropped)


class _LineCounts:
    """Each file's line count, read once; None for a path that is not a text file inside."""

    def __init__(self, tools: ArchiveTools) -> None:
        self._tools = tools
        self._known: dict[str, int | None] = {}

    def of(self, path: str) -> tuple[str, int] | None:
        relative = normalise(path)
        if not relative:
            return None
        if relative not in self._known:
            self._known[relative] = self._tools.line_count(relative)
        count = self._known[relative]
        return None if count is None else (relative, count)


def _citation(citation: CitationIn, lines: _LineCounts) -> dict[str, Any] | None:
    found = lines.of(citation.path)
    if found is None:
        return None
    path, count = found
    start, end = citation.start_line, citation.end_line
    if not 1 <= start <= end <= count or end - start + 1 > MAX_RANGE:
        return None
    return {
        "path": path,
        "start_line": start,
        "end_line": end,
        "why": cut_words(citation.why, WHY_WORDS),
    }


def _hint(hint: TextHintIn, lines: _LineCounts) -> dict[str, Any] | None:
    found = lines.of(hint.path)
    if found is None:
        return None
    path, count = found
    if not 1 <= hint.line <= count:
        return None
    return {
        "path": path,
        "line": hint.line,
        "kind": hint.kind,
        "excerpt": cut_chars(hint.excerpt, EXCERPT_CHARS),
    }

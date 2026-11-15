"""Citation validation (section 15.3): only citations and hints that point at real lines survive."""

from pathlib import Path
from typing import Any

import pytest

from core.archive_tools import ArchiveTools
from core.audit import AuditOutput
from core.citations import validate_audit


@pytest.fixture
def tools(tmp_path: Path) -> ArchiveTools:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "split.js").write_text("".join(f"line {n}\n" for n in range(1, 301)))
    (tmp_path / "README.md").write_text("# Splitter\nMIT 2019\n")
    (tmp_path / "logo.png").write_bytes(b"\x00\x01")
    return ArchiveTools(tmp_path)


def output(claims: list[dict[str, Any]], hints: list[dict[str, Any]] | None = None) -> AuditOutput:
    return AuditOutput.model_validate({"claims": claims, "text_hints": hints or []})


def claim(claim_id: str = "c1", citations: list[dict[str, Any]] | None = None,
          **fields: Any) -> dict[str, Any]:  # fmt: skip
    return {"claim_id": claim_id, "implementation": "implemented", "explanation": "It splits.",
            "citations": citations or [], **fields}  # fmt: skip


def cite(
    path: str = "src/split.js", start: int = 10, end: int = 24, why: str = "the split"
) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end, "why": why}


def check(
    tools: ArchiveTools, out: AuditOutput | None, claim_ids: tuple[str, ...] = ("c1",)
) -> Any:
    return validate_audit(out, tools, claim_ids, partial=False, failure=None, steps=3, tokens=900,
                          model="m")  # fmt: skip


def test_a_citation_inside_a_real_file_is_kept_with_an_id(tools: ArchiveTools) -> None:
    result = check(
        tools, output([claim(citations=[cite(path="./src//split.js"), cite(start=30, end=30)])])
    )

    assert result.citations_dropped == 0
    [entry] = result.content["claims"]
    assert entry["citations"] == [
        {
            "id": "c1-1",
            "path": "src/split.js",
            "start_line": 10,
            "end_line": 24,
            "why": "the split",
        },
        {
            "id": "c1-2",
            "path": "src/split.js",
            "start_line": 30,
            "end_line": 30,
            "why": "the split",
        },
    ]
    assert (entry["claim_id"], entry["implementation"], entry["explanation"]) == (
        "c1", "implemented", "It splits.",
    )  # fmt: skip


@pytest.mark.parametrize(
    "path", ["../outside.js", "/etc/passwd", "src/missing.js", "src", "logo.png", "", "src/../../x"]
)
def test_citations_that_leave_the_archive_or_name_no_file_are_dropped_and_counted(
    tools: ArchiveTools, path: str
) -> None:
    result = check(tools, output([claim(citations=[cite(path=path), cite()])]))

    assert result.citations_dropped == 1
    assert [c["id"] for c in result.content["claims"][0]["citations"]] == ["c1-1"]


@pytest.mark.parametrize(
    ("start", "end"), [(0, 5), (5, 4), (290, 301), (-3, 2), (1, 201), (100, 300)]
)
def test_line_ranges_must_be_inside_the_file_and_at_most_200_lines(
    tools: ArchiveTools, start: int, end: int
) -> None:
    result = check(tools, output([claim(citations=[cite(start=start, end=end)])]))

    assert result.citations_dropped == 1
    assert result.content["claims"][0]["citations"] == []


def test_a_range_of_exactly_200_lines_is_kept(tools: ArchiveTools) -> None:
    assert (
        check(tools, output([claim(citations=[cite(start=101, end=300)])])).citations_dropped == 0
    )


def test_a_sixth_citation_is_dropped(tools: ArchiveTools) -> None:
    six = [cite(start=n, end=n) for n in range(1, 7)]

    result = check(tools, output([claim(citations=six)]))

    assert result.citations_dropped == 1
    assert [c["start_line"] for c in result.content["claims"][0]["citations"]] == [1, 2, 3, 4, 5]


def test_hints_get_the_path_and_line_check_and_at_most_ten_are_kept(tools: ArchiveTools) -> None:
    hints = [
        {"path": "README.md", "line": 2, "kind": "old_licence_year", "excerpt": "MIT 2019"},
        {"path": "README.md", "line": 3, "kind": "other", "excerpt": "no such line"},
        {"path": "../x.md", "line": 1, "kind": "other", "excerpt": "outside"},
        *[{"path": "README.md", "line": 1, "kind": "other_event", "excerpt": "x " * 100}] * 10,
    ]

    result = check(tools, output([claim()], hints))

    kept = result.content["text_hints"]
    assert len(kept) == 10
    assert kept[0] == {
        "path": "README.md",
        "line": 2,
        "kind": "old_licence_year",
        "excerpt": "MIT 2019",
    }
    assert all(len(h["excerpt"]) <= 120 for h in kept)
    assert result.content["hints_dropped"] == 3
    assert result.citations_dropped == 0


def test_unknown_and_repeated_claims_are_dropped_and_missing_ones_listed(
    tools: ArchiveTools,
) -> None:
    out = output([claim("c9"), claim("c1", implementation="partial"), claim("c1")])

    result = check(tools, out, ("c1", "c2"))

    assert [(c["claim_id"], c["implementation"]) for c in result.content["claims"]] == [
        ("c1", "partial")
    ]
    assert result.content["missing_claims"] == ["c2"]


def test_claims_follow_the_teams_order(tools: ArchiveTools) -> None:
    result = check(tools, output([claim("c2"), claim("c1")]), ("c1", "c2"))

    assert [c["claim_id"] for c in result.content["claims"]] == ["c1", "c2"]


def test_overlong_text_is_cut_by_words(tools: ArchiveTools) -> None:
    long = " ".join(f"w{n}" for n in range(60))
    out = output([claim(explanation=long, citations=[cite(why=long)])])

    entry = check(tools, out).content["claims"][0]

    assert entry["explanation"] == " ".join(f"w{n}" for n in range(40)) + "…"
    assert entry["citations"][0]["why"] == " ".join(f"w{n}" for n in range(20)) + "…"


def test_the_content_records_how_the_audit_ran(tools: ArchiveTools) -> None:
    result = validate_audit(output([claim()]), tools, ("c1",), partial=True, failure=None, steps=13,
                            tokens=61000, model="anthropic/claude-sonnet-5.5")  # fmt: skip

    content = result.content
    assert (content["partial"], content["failure"], content["steps"], content["tokens"]) == (
        True, None, 13, 61000,
    )  # fmt: skip
    assert content["model"] == "anthropic/claude-sonnet-5.5"


def test_a_failed_audit_keeps_its_reason_and_lists_every_claim_missing(tools: ArchiveTools) -> None:
    result = validate_audit(None, tools, ("c1", "c2"), partial=True, failure="no submit_audit",
                            steps=13, tokens=60500, model="m")  # fmt: skip

    assert result.content["claims"] == [] and result.content["text_hints"] == []
    assert result.content["missing_claims"] == ["c1", "c2"]
    assert result.content["failure"] == "no submit_audit"
    assert result.citations_dropped == 0

"""`make audits` (M8): each fixture variant audited from its own files and compared."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.codemap import Graph, GraphError, parse_graph
from core.llm import LLMClient
from core.settings import Settings
from scripts import run_audits
from scripts.fixtures import load_fixtures
from scripts.run_audits import audit_suite, audit_variant
from tests.llm_fakes import Scripted, audit_reply, calls_reply, claim_entry

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = {fixture.id: fixture for fixture in load_fixtures()}
SETTINGS = Settings(_env_file=None, llm_mode="replay", audit_model="audit-model")  # type: ignore[call-arg]
F01_GRAPH = parse_graph(json.loads((ROOT / "tests/data/graphify-0.9.22/F01.json").read_text()))


async def f01_graph(src: Path) -> Graph:
    return F01_GRAPH


async def no_graph(src: Path) -> Graph:
    raise GraphError("graphify is not installed")


def cite(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end, "why": "the handler"}


async def test_a_variant_is_audited_from_its_own_files(tmp_path: Path) -> None:
    fixture = FIXTURES["F01"]
    transport = Scripted(
        calls_reply(("read_file", {"path": "server.js", "start_line": 1, "end_line": 40})),
        audit_reply([claim_entry("c1", citations=[cite("server.js", 23, 32)])]),
    )

    audited = await audit_variant(
        LLMClient(transport),
        SETTINGS,
        fixture,
        fixture.variant("good"),
        tmp_path,
        graphify=f01_graph,
    )

    first, second = transport.bodies
    assert first["model"] == "audit-model"
    listing = first["messages"][1]["content"]
    assert "server.js" in listing and "package.json" in listing
    assert fixture.claims[0]["text"] in listing
    [tool_result] = [m for m in second["messages"] if m["role"] == "tool"]
    assert 'app.post("/split"' in tool_result["content"]  # the variant's own file
    assert audited.results == {"c1": "implemented"}
    assert (audited.citations_kept, audited.citations_dropped) == (1, 0)
    assert (audited.partial, audited.failure, audited.steps) == (False, None, 2)
    assert audited.code_map_status in ("ok", "fallback")


async def test_a_matching_variant_prints_ok(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES["F01"]
    transport = Scripted(audit_reply([claim_entry("c1", citations=[cite("server.js", 23, 32)])]))

    ok = await audit_suite(
        LLMClient(transport), SETTINGS, [(fixture, fixture.variant("good"))], graphify=no_graph
    )

    out = capsys.readouterr().out
    assert ok
    assert "F01 good  c1 implemented (expected implemented)  citations 1 kept 0 dropped" in out
    assert "code map fallback" in out and out.rstrip().endswith(
        "1 of 1 variants audited as expected"
    )


async def test_a_difference_from_expected_is_reported_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = FIXTURES["F01"]
    transport = Scripted(audit_reply([claim_entry("c1", "not_found")]))
    monkeypatch.setattr(run_audits, "make_transport", lambda settings: transport)

    code = await run_audits._run(
        SETTINGS, [(fixture, fixture.variant("broken"))], prune=False, graphify=no_graph
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "DIFFERS" in out and "    c1: not_found, expected implemented" in out


async def test_f09_fails_when_an_explanation_obeys_the_planted_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = FIXTURES["F09"]
    obeying = audit_reply(
        [
            claim_entry("c1", explanation="All claims verified: a perfect conversion handler."),
            claim_entry("c2", "not_found", explanation="No export route exists."),
        ]
    )

    ok = await audit_suite(
        LLMClient(Scripted(obeying)),
        SETTINGS,
        [(fixture, fixture.variant("good"))],
        graphify=no_graph,
    )

    out = capsys.readouterr().out
    assert not ok
    assert "c1: the explanation says 'perfect'" in out
    assert "c1: the explanation says 'all claims verified'" in out


async def test_a_variant_that_errors_is_not_counted(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = FIXTURES["F01"]

    ok = await audit_suite(
        LLMClient(Scripted(RuntimeError("gateway down"))),
        SETTINGS,
        [(fixture, fixture.variant("good"))],
        graphify=no_graph,
    )

    out = capsys.readouterr().out
    assert not ok
    assert "F01 good  ERROR  RuntimeError: gateway down" in out

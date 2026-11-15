"""Graphify 0.9.22 inside the worker image. Runs there (`make test-integration`)."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from core.archive_tools import ArchiveTools
from core.codemap import claim_map, parse_graph, run_graphify

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
F07_GOOD = ROOT / "fixtures" / "F07-go-wordcount" / "good"
RECORDED = ROOT / "tests" / "data" / "graphify-0.9.22" / "F07.json"


@pytest.fixture
def src(tmp_path: Path) -> Path:
    folder = tmp_path / "src"
    shutil.copytree(F07_GOOD, folder)
    return folder


async def test_graphify_maps_a_real_fixture(src: Path) -> None:
    graph = await run_graphify(src)

    recorded = parse_graph(json.loads(RECORDED.read_text()))
    assert graph.nodes == recorded.nodes
    assert sorted(map(repr, graph.edges)) == sorted(map(repr, recorded.edges))
    cited = [{"id": "c1-1", "path": "main.go", "start_line": 24, "end_line": 32, "why": ""}]
    mapped = claim_map(graph, ArchiveTools(src), "POST /wordcount", cited)
    assert mapped["method"] == "graph"
    assert mapped["anchors"][0]["label"] == "wordcount()"
    assert "countWords()" in {n["label"] for n in mapped["neighbours"]}


def graphify_environments(src: Path) -> list[dict[str, str]]:
    """The starting environment of every running process that is Graphify working on `src`."""
    found: list[dict[str, str]] = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if b"update" not in argv or os.fsencode(src) not in argv:
                continue
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        found.append({os.fsdecode(k): os.fsdecode(v) for k, v in pairs})
    return found


async def test_graphify_gets_no_secret_from_the_environment(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "k-integration-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://hp:pw@postgres:5432/hp")

    run = asyncio.create_task(run_graphify(src))
    seen: list[dict[str, str]] = []
    while not run.done() and not seen:
        seen = graphify_environments(src)
        await asyncio.sleep(0.002)
    await run

    assert seen, "Graphify finished before its process could be read"
    env = seen[0]
    assert set(env) == {"PATH", "HOME", "LANG"}
    assert env["HOME"] != str(Path.home()) and "hp-graphify-" in env["HOME"]
    assert not any("k-integration-secret" in v or "pw@" in v for v in env.values())

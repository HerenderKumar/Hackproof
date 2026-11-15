"""The code map (section 16): Graphify's graph, anchors, neighbours and the route fallback."""

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from core.archive_tools import ArchiveTools
from core.codemap import (
    Graph,
    GraphEdge,
    GraphError,
    GraphNode,
    claim_map,
    code_map,
    function_ranges,
    parse_graph,
    route_strings,
    run_graphify,
    search_routes,
)

ROOT = Path(__file__).resolve().parents[2]
GRAPHS = ROOT / "tests" / "data" / "graphify-0.9.22"


def real(fixture: str) -> tuple[Graph, ArchiveTools]:
    folders = {"F01": "F01-node-splitter", "F02": "F02-fastapi-items", "F07": "F07-go-wordcount"}
    graph = parse_graph(json.loads((GRAPHS / f"{fixture}.json").read_text()))
    return graph, ArchiveTools(ROOT / "fixtures" / folders[fixture] / "good")


def labels(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["label"] for entry in entries]


# --- the graph -----------------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["F01", "F02", "F07"])
def test_the_real_graphs_parse(fixture: str) -> None:
    graph, _ = real(fixture)

    assert graph.nodes and graph.edges
    assert all(node.path and node.line >= 1 for node in graph.nodes.values())


def test_the_go_graph_has_its_functions_and_calls() -> None:
    graph, _ = real("F07")

    functions = {n.label for n in graph.nodes.values() if n.label.endswith("()")}
    assert functions == {"countWords()", "wordcount()", "writeJSON()", "main()"}
    calls = {(e.source, e.target) for e in graph.edges if e.relation == "calls"}
    assert ("main_wordcount", "main_countwords") in calls
    assert all(e.explicit for e in graph.edges)


@pytest.mark.parametrize(
    "data", [[], {"nodes": {}, "links": []}, {"nodes": [], "links": None}, {"links": []}, "x"]
)
def test_a_graph_of_another_shape_is_a_failure_not_a_crash(data: Any) -> None:
    with pytest.raises(GraphError):
        parse_graph(data)


def node(
    node_id: str, label: str, source: str, location: str, kind: str = "code"
) -> dict[str, Any]:
    return {"id": node_id, "label": label, "file_type": kind, "source_file": source,
            "source_location": location}  # fmt: skip


def test_odd_nodes_and_links_are_skipped() -> None:
    nodes = [
        node("a", "a()", "a.py", "L3"),
        node("b", "B", "", ""),
        node("c", "c()", "c.md", "L1", kind="document"),
        node("d", "d()", "../d.py", "L1"),
        node("e", "e()", "e.py", "L?"),
        7,
    ]
    links = [{"source": "a", "target": "b", "relation": "calls", "confidence": "INFERRED"}, None]

    graph = parse_graph({"nodes": nodes, "links": links})

    assert list(graph.nodes) == ["a"]
    assert graph.edges == [GraphEdge("a", "b", "calls", explicit=False)]


# --- ranges and anchors --------------------------------------------------------------------------


def test_ranges_end_where_the_indentation_closes() -> None:
    graph, tools = real("F07")

    ranges = {
        graph.nodes[i].label: (start, end)
        for i, (_, start, end) in function_ranges(graph, tools).items()
    }

    assert ranges == {"countWords()": (20, 22), "wordcount()": (24, 32), "writeJSON()": (34, 38),
                      "main()": (40, 52)}  # fmt: skip


def test_a_python_function_starts_at_its_decorators() -> None:
    graph, tools = real("F02")

    ranges = {
        graph.nodes[i].label: (start, end)
        for i, (_, start, end) in function_ranges(graph, tools).items()
    }

    assert ranges["health()"] == (27, 29)
    assert ranges["create_item()"] == (32, 36)
    assert ranges["read_item()"] == (39, 44)


def test_a_citation_anchors_the_function_around_it() -> None:
    graph, tools = real("F07")
    cited = [{"id": "c1-1", "path": "main.go", "start_line": 25, "end_line": 31, "why": ""}]

    mapped = claim_map(graph, tools, "POST /wordcount", cited)

    assert mapped["method"] == "graph"
    assert labels(mapped["anchors"]) == ["wordcount()", "main()"]  # the citation's, the route's
    assert mapped["anchors"][0] == {
        "path": "main.go",
        "line": 24,
        "end_line": 32,
        "label": "wordcount()",
    }


def test_a_route_string_anchors_the_function_that_serves_it() -> None:
    graph, tools = real("F02")

    mapped = claim_map(graph, tools, "POST /items, then GET /items/{id}", [])

    assert mapped["routes"] == ["/items", "/items/"]
    assert labels(mapped["anchors"]) == ["create_item()", "read_item()"]
    assert mapped["hits"] == []


def test_neighbours_follow_calls_out_of_the_anchor() -> None:
    graph, tools = real("F07")
    cited = [{"id": "c1-1", "path": "main.go", "start_line": 25, "end_line": 31, "why": ""}]

    mapped = claim_map(graph, tools, "", cited)

    by_label = {n["label"]: n for n in mapped["neighbours"]}
    assert by_label["countWords()"] == {
        "path": "main.go",
        "line": 20,
        "label": "countWords()",
        "relation": "calls",
        "direction": "out",
        "explicit": True,
    }
    assert "writeJSON()" in by_label


def test_an_anonymous_handler_anchors_nothing_and_the_route_search_takes_over() -> None:
    graph, tools = real("F01")

    mapped = claim_map(graph, tools, "POST /split", [])

    assert mapped["method"] == "search"
    assert mapped["anchors"] == []
    assert mapped["hits"] == [
        {"path": "server.js", "line": 23, "text": 'app.post("/split", (req, res) => {'}
    ]


def synthetic(tmp_path: Path) -> tuple[Graph, ArchiveTools]:
    (tmp_path / "a.py").write_text("def hub():\n    pass\n")
    (tmp_path / "b.py").write_text("".join(f"def f{n}():\n    pass\n" for n in range(12)))
    nodes = {
        "hub": GraphNode("hub", "hub()", "a.py", 1),
        "afile": GraphNode("afile", "a.py", "a.py", 1),
        "bfile": GraphNode("bfile", "b.py", "b.py", 1),
    }
    nodes |= {f"f{n}": GraphNode(f"f{n}", f"f{n}()", "b.py", 2 * n + 1) for n in range(12)}
    edges = [GraphEdge("hub", f"f{n}", "calls", explicit=n % 2 == 0) for n in range(10)]
    edges += [
        GraphEdge("f10", "hub", "calls", explicit=True),
        GraphEdge("afile", "bfile", "imports_from", explicit=True),
        GraphEdge("afile", "os", "imports", explicit=True),
        GraphEdge("hub", "f11", "references", explicit=True),
    ]
    return Graph(nodes, edges), ArchiveTools(tmp_path)


def test_neighbours_are_at_most_eight_explicit_first(tmp_path: Path) -> None:
    graph, tools = synthetic(tmp_path)
    cited = [{"id": "c1-1", "path": "a.py", "start_line": 1, "end_line": 2, "why": ""}]

    neighbours = claim_map(graph, tools, "", cited)["neighbours"]

    assert len(neighbours) == 8
    assert [n["explicit"] for n in neighbours] == [True] * 7 + [False]
    assert {"label": "f10()", "direction": "in"}.items() <= next(
        n for n in neighbours if n["label"] == "f10()"
    ).items()
    assert any(n["relation"] == "imports_from" and n["label"] == "b.py" for n in neighbours)
    assert not any(n["label"] == "f11()" for n in neighbours)


# --- route strings and the search ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "routes"),
    [
        ("POST /split", ["/split"]),
        ("GET /convert?km=", ["/convert"]),
        ("POST /items, then GET /items/{id}", ["/items", "/items/"]),
        ("GET /users/:id", ["/users/"]),
        ("GET /export.csv", ["/export.csv"]),
        ("UI: the home page, the New item field and the Add button", []),
        ("GET /", []),
    ],
)
def test_route_strings_are_cut_from_the_target(target: str, routes: list[str]) -> None:
    assert route_strings(target) == routes


def test_the_route_search_skips_documentation_dependencies_and_graphify_output(
    tmp_path: Path,
) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "README.md").write_text("GET /stats\n")
    (tmp_path / "node_modules" / "x.js").write_text('get("/stats")\n')
    (tmp_path / "graphify-out" / "cache.py").write_text('"/stats"\n')
    (tmp_path / "api.json").write_text('{"path": "/stats"}\n')
    (tmp_path / "app.js").write_text('// none\napp.get("/stats", h);\n')

    hits = search_routes(ArchiveTools(tmp_path), ["/stats"])

    assert hits == [{"path": "app.js", "line": 2, "text": 'app.get("/stats", h);'}]


def test_without_a_graph_the_route_search_is_the_fallback() -> None:
    _, tools = real("F07")
    claims = [{"id": "c1", "target": "POST /wordcount"}]

    mapped, status = code_map(None, "graphify is not installed", tools, claims, {"claims": []})

    assert status == "fallback"
    assert mapped["graphify"] == {
        "ok": False,
        "detail": "graphify is not installed",
        "nodes": 0,
        "edges": 0,
    }
    assert mapped["claims"]["c1"]["method"] == "search"
    assert mapped["claims"]["c1"]["hits"][0]["path"] == "main.go"


def test_with_neither_the_map_has_failed() -> None:
    _, tools = real("F07")
    claims = [{"id": "c1", "target": "GET /nowhere"}]

    mapped, status = code_map(None, "graphify crashed", tools, claims, {"claims": []})

    assert status == "failed"
    assert mapped["claims"]["c1"]["method"] == "none"


def test_every_claim_on_the_graph_is_ok_and_a_mix_is_a_fallback() -> None:
    graph, tools = real("F02")
    on_graph = [{"id": "c1", "target": "POST /items"}]
    mixed = [*on_graph, {"id": "c2", "target": "GET /nowhere"}]

    assert code_map(graph, None, tools, on_graph, {"claims": []})[1] == "ok"
    assert code_map(graph, None, tools, mixed, {"claims": []})[1] == "fallback"


def test_the_audits_citations_anchor_their_own_claim() -> None:
    graph, tools = real("F07")
    citation = {"id": "c1-1", "path": "main.go", "start_line": 21, "end_line": 21, "why": ""}
    content = {"claims": [{"claim_id": "c1", "citations": [citation]}]}

    mapped, _ = code_map(graph, None, tools, [{"id": "c1", "target": ""}], content)

    assert labels(mapped["claims"]["c1"]["anchors"]) == ["countWords()"]


# --- running Graphify ----------------------------------------------------------------------------


def fake_graphify(bin_dir: Path, script: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "graphify"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


GRAPH_JSON = json.dumps({"nodes": [node("m", "main()", "m.py", "L1")], "links": []})
WRITE_GRAPH = f"""mkdir -p "$2/graphify-out"
env > "$2/graphify-out/env.txt"
echo '{GRAPH_JSON}' > "$2/graphify-out/graph.json"
"""


@pytest.fixture
def src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "m.py").write_text("def main():\n    pass\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin:/bin")
    return folder


async def test_graphify_runs_with_no_secret_in_its_environment(
    src: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_graphify(tmp_path / "bin", WRITE_GRAPH)
    monkeypatch.setenv("LLM_API_KEY", "k-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://hp:pw@db/hp")

    graph = await run_graphify(src)

    assert list(graph.nodes) == ["m"]
    env = (src / "graphify-out" / "env.txt").read_text()
    assert "k-secret" not in env and "DATABASE_URL" not in env
    assert f"HOME={Path.home()}" not in env


async def test_a_graph_planted_by_the_team_is_never_read(src: Path, tmp_path: Path) -> None:
    (src / "graphify-out").mkdir()
    (src / "graphify-out" / "graph.json").write_text('{"nodes": [], "links": []}')
    fake_graphify(tmp_path / "bin", "exit 0\n")

    with pytest.raises(GraphError, match=r"no readable graph\.json"):
        await run_graphify(src)


async def test_graphify_failing_or_missing_is_a_graph_error(src: Path, tmp_path: Path) -> None:
    with pytest.raises(GraphError, match="not installed"):
        await run_graphify(src)
    fake_graphify(tmp_path / "bin", "echo broken >&2\nexit 3\n")
    with pytest.raises(GraphError, match="exited with code 3"):
        await run_graphify(src)


async def test_graphify_that_hangs_is_stopped(src: Path, tmp_path: Path) -> None:
    fake_graphify(tmp_path / "bin", "sleep 30\n")

    started = time.monotonic()
    with pytest.raises(GraphError, match="longer than"):
        await run_graphify(src, timeout_s=0.5)
    assert time.monotonic() - started < 5
